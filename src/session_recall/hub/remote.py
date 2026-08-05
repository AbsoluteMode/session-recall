"""A Recall that lives on the hub.

Implements the same five operations as the local `Recall`, over HTTP, so
`server.py` can hand either one to the MCP tools without knowing which. The
agent-facing contract does not change: same tool names, same arguments, same
shapes coming back.

Why proxy at all, instead of pointing the MCP host straight at a remote
server: the key stays in a 0600 file managed by `hub join` rather than in the
host's config, the plugin installs unchanged, and a hub that is down produces
one clear sentence instead of a transport error the agent has to interpret.

Date arguments are resolved to epochs HERE, on the machine that knows the
user's timezone, and sent as numbers. A server guessing at "yesterday" for
someone three timezones away would be quietly wrong.
"""

import gzip
import json
import urllib.error
import urllib.request

from ..models import Anchor, Turn
from ..retrieve import SearchResult
from .client import HubConfig, HubError

_TIMEOUT_S = 60


class RemoteRecall:
    def __init__(self, cfg: HubConfig, timeout: int = _TIMEOUT_S):
        self.cfg = cfg
        self.timeout = timeout

    def _post(self, path: str, body: dict) -> dict:
        payload = gzip.compress(json.dumps(body).encode())
        request = urllib.request.Request(
            self.cfg.url + path, data=payload,
            headers={"Content-Type": "application/json",
                     "Content-Encoding": "gzip",
                     "Authorization": f"Bearer {self.cfg.key}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as failure:
            if failure.code == 401:
                raise HubError("hub rejected this machine's key — ask the "
                               "operator to issue a new one") from failure
            raise HubError(f"hub error {failure.code}") from failure
        except urllib.error.URLError as failure:
            raise HubError(f"hub unreachable: {failure.reason}") from failure

    @staticmethod
    def _anchors(rows: list) -> list[Anchor]:
        fields = Anchor.__dataclass_fields__
        return [Anchor(**{k: v for k, v in row.items() if k in fields})
                for row in rows]

    @staticmethod
    def _turns(rows: list) -> list[Turn]:
        fields = Turn.__dataclass_fields__
        return [Turn(**{k: v for k, v in row.items() if k in fields})
                for row in rows]

    def recall_search(self, query: str, k: int = 10, scope_cwd: str | None = None,
                      source: str | None = None, start_ts: int | None = None,
                      end_ts: int | None = None, **_ignored) -> SearchResult:
        body = self._post("/v1/search", {
            "query": query, "k": k, "scope_cwd": scope_cwd, "source": source,
            "start_ts": start_ts, "end_ts": end_ts})
        return SearchResult(self._anchors(body.get("anchors", [])),
                            body.get("degraded"))

    def expand_around(self, session_id: str, uuid: str, before: int = 2,
                      after: int = 2, source: str | None = None) -> list[Turn]:
        return self._turns(self._post("/v1/expand", {
            "session_id": session_id, "uuid": uuid, "before": before,
            "after": after, "source": source}).get("turns", []))

    def step(self, session_id: str, uuid: str, direction: str, count: int = 1,
             source: str | None = None) -> list[Turn]:
        return self._turns(self._post("/v1/step", {
            "session_id": session_id, "uuid": uuid, "direction": direction,
            "count": count, "source": source}).get("turns", []))

    def grep(self, pattern: str, session_id: str | None = None,
             scope_cwd: str | None = None, source: str | None = None,
             limit: int = 100, start_ts: int | None = None,
             end_ts: int | None = None) -> list[Anchor]:
        return self._anchors(self._post("/v1/grep", {
            "pattern": pattern, "session_id": session_id, "scope_cwd": scope_cwd,
            "source": source, "limit": limit,
            "start_ts": start_ts, "end_ts": end_ts}).get("anchors", []))

    def recent_sessions(self, scope_cwd: str | None = None, limit: int = 10,
                        source: str | None = None, start_ts: int | None = None,
                        end_ts: int | None = None) -> list[dict]:
        return self._post("/v1/recent", {
            "scope_cwd": scope_cwd, "limit": limit, "source": source,
            "start_ts": start_ts, "end_ts": end_ts}).get("sessions", [])

    def ask(self, question: str, k: int = 12,
            scope_cwd: str | None = None) -> dict:
        return self._post("/v1/ask", {
            "question": question, "k": k, "scope_cwd": scope_cwd})
