"""The hub over real HTTP: ThreadingHTTPServer on an ephemeral port, urllib as
the client — the same shape as the relay suite."""

import base64
import gzip
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from session_recall.hub import storage
from session_recall.hub.app import Hub, make_handler
from session_recall.hub.masking import SecretMap
from session_recall.models import Anchor

GOOD = "claude/-Users-egor-proj/sess.jsonl"
NETCUP = "Xk39dmPQ7wLz2vRt"


class FakeRecall:
    def __init__(self):
        self.calls = []

    def recall_search(self, query, **kw):
        self.calls.append((query, kw))
        return [Anchor(session_id="s1", uuid="u1", role="assistant",
                       snippet="we pinned the version", score=0.9,
                       project="proj", when=1785000000, source="claude")]


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path / "hub", recall_factory=FakeRecall)


@pytest.fixture
def server(hub):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(hub))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def post(url, path, body, key=None, compress=False):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if compress:
        raw, headers["Content-Encoding"] = gzip.compress(raw), "gzip"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url + path, data=raw, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def upload(url, key, data: bytes, offset=0, path=GOOD, **extra):
    return post(url, "/v1/ingest",
                {"path": path, "offset": offset,
                 "data": base64.b64encode(data).decode(), **extra}, key=key)


def test_healthz_needs_no_key_and_says_nothing_about_content(server):
    with urllib.request.urlopen(server + "/healthz", timeout=10) as response:
        assert json.loads(response.read()) == {"ok": True}


def test_every_data_route_requires_a_key(server):
    for path in ("/v1/ingest/manifest", "/v1/ingest", "/v1/search"):
        with pytest.raises(urllib.error.HTTPError) as raised:
            post(server, path, {})
        assert raised.value.code == 401


def test_a_revoked_key_stops_working_immediately(server, hub):
    key = hub.keys.issue("egor")
    assert post(server, "/v1/ingest/manifest", {}, key=key)[0] == 200
    hub.keys.revoke("egor")
    with pytest.raises(urllib.error.HTTPError) as raised:
        post(server, "/v1/ingest/manifest", {}, key=key)
    assert raised.value.code == 401


def test_unknown_route_is_404(server, hub):
    with pytest.raises(urllib.error.HTTPError) as raised:
        post(server, "/v1/whatever", {}, key=hub.keys.issue("egor"))
    assert raised.value.code == 404


def test_upload_then_manifest_roundtrip(server, hub):
    key = hub.keys.issue("egor")
    assert upload(server, key, b"line1\n") == (200, {"size": 6, "masked": 0})
    assert upload(server, key, b"line2\n", offset=6)[1]["size"] == 12
    assert post(server, "/v1/ingest/manifest", {}, key=key)[1] == {"files": {GOOD: 12}}
    stored = storage.resolve(hub.transcripts, "egor", GOOD)
    assert stored.read_bytes() == b"line1\nline2\n"


def test_uploads_land_in_the_sender_s_own_tree(server, hub):
    """The owner comes from the key, so a member cannot write into another
    member's history no matter what the body says."""
    egor, maxim = hub.keys.issue("egor"), hub.keys.issue("maxim")
    upload(server, egor, b"egor\n")
    upload(server, maxim, b"maxim\n")
    assert storage.resolve(hub.transcripts, "egor", GOOD).read_bytes() == b"egor\n"
    assert storage.resolve(hub.transcripts, "maxim", GOOD).read_bytes() == b"maxim\n"


def test_traversal_is_refused(server, hub):
    key = hub.keys.issue("egor")
    with pytest.raises(urllib.error.HTTPError) as raised:
        upload(server, key, b"x", path="claude/../../../etc/passwd.jsonl")
    assert raised.value.code == 400


def test_offset_mismatch_tells_the_client_where_to_resume(server, hub):
    key = hub.keys.issue("egor")
    upload(server, key, b"line1\n")
    with pytest.raises(urllib.error.HTTPError) as raised:
        upload(server, key, b"late\n", offset=99)
    assert raised.value.code == 409
    assert json.loads(raised.value.read())["size"] == 6


def test_gzipped_bodies_are_accepted(server, hub):
    key = hub.keys.issue("egor")
    status, body = post(server, "/v1/ingest",
                        {"path": GOOD, "offset": 0,
                         "data": base64.b64encode(b"compressed\n").decode()},
                        key=key, compress=True)
    assert (status, body["size"]) == (200, 11)


def test_known_secrets_never_reach_the_disk(server, hub):
    """The masking map is live at ingest, so a credential in a transcript is
    replaced before the bytes are stored — not after someone notices."""
    SecretMap.build({"servers/NETCUP_PASSWORD": NETCUP},
                    salt="test-salt").save(hub.secrets_path)
    key = hub.keys.issue("egor")
    line = json.dumps({"text": f"ssh with {NETCUP}"}).encode() + b"\n"
    _, body = upload(server, key, line)

    stored = storage.resolve(hub.transcripts, "egor", GOOD).read_bytes()
    assert NETCUP.encode() not in stored
    assert b"${servers/NETCUP_PASSWORD}" in stored
    assert body["masked"] == 1
    # The manifest counts the CLIENT's bytes, so the next tail lines up even
    # though masking changed the stored length.
    assert body["size"] == len(line) != len(stored)


def test_search_returns_ranked_anchors(server, hub):
    key = hub.keys.issue("egor")
    status, body = post(server, "/v1/search",
                        {"query": "how did we pin it", "k": 3}, key=key)
    assert status == 200
    assert body["anchors"][0]["snippet"] == "we pinned the version"
    assert body["anchors"][0]["when_human"]           # humanised for the agent
    assert body["degraded"] is None


def test_search_without_a_query_is_a_bad_request(server, hub):
    with pytest.raises(urllib.error.HTTPError) as raised:
        post(server, "/v1/search", {"k": 3}, key=hub.keys.issue("egor"))
    assert raised.value.code == 400


def test_oversized_body_is_refused(server, hub):
    key = hub.keys.issue("egor")
    request = urllib.request.Request(
        server + "/v1/ingest", data=b"{}",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Length": str(64 * 1024 * 1024)})
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(request, timeout=10)
    assert raised.value.code == 400
