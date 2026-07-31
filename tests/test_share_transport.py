"""Transport selection: explicit env wins, the public relay is the default
(zero-config onboarding), and `none` opts out of the network entirely."""

from session_recall.share.transport import (
    DEFAULT_RELAY_URL, FileTransport, HttpRelayTransport, from_env)


def test_default_is_the_public_relay():
    t = from_env({})
    assert isinstance(t, HttpRelayTransport)
    assert t.base == DEFAULT_RELAY_URL.rstrip("/")


def test_explicit_url_wins(tmp_path):
    t = from_env({"SESSION_RECALL_RELAY_URL": "https://my.example",
                  "SESSION_RECALL_SHARE_TRANSPORT_DIR": str(tmp_path)})
    assert isinstance(t, HttpRelayTransport) and t.base == "https://my.example"


def test_shared_dir_beats_the_default(tmp_path):
    assert isinstance(from_env(
        {"SESSION_RECALL_SHARE_TRANSPORT_DIR": str(tmp_path)}), FileTransport)


def test_none_disables_the_relay(tmp_path):
    for v in ("none", "off", "NONE", " none "):
        assert from_env({"SESSION_RECALL_RELAY_URL": v}) is None
    # …but an explicit shared folder still works alongside the opt-out
    t = from_env({"SESSION_RECALL_RELAY_URL": "none",
                  "SESSION_RECALL_SHARE_TRANSPORT_DIR": str(tmp_path)})
    assert isinstance(t, FileTransport)
