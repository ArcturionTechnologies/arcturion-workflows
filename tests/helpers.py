"""Shared fakes. The fake n8n server is a dict in memory; nothing opens a socket."""
import io
import json
import socket
import sys
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class OfflineTestCase(unittest.TestCase):
    """Fails loudly if any test tries to open a real network connection."""

    def setUp(self):
        def _blocked(*a, **k):
            raise AssertionError("test attempted real network access")
        p = unittest.mock.patch.object(socket.socket, "connect", _blocked)
        p.start()
        self.addCleanup(p.stop)


class FakeN8n:
    """In-memory n8n Public API. Records every call as (method, path, body)."""

    def __init__(self, workflows=None, page_size=100, fail=None):
        self.workflows = {w["id"]: dict(w) for w in (workflows or [])}
        self.calls = []
        self.page_size = page_size
        self.fail = fail or {}       # (method, path-prefix) -> status
        self.next_id = 1000
        self.last_headers = None

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("/api/v1", 1)[1]
        data = json.loads(body) if body else None
        self.calls.append((method, path, data))
        self.last_headers = headers
        for (m, prefix), status in self.fail.items():
            if m == method and path.startswith(prefix):
                return status, b'{"message": "forced failure"}'
        route = path.split("?")[0]
        if method == "GET" and route == "/workflows":
            items = sorted(self.workflows.values(), key=lambda w: w["id"])
            qs = dict(p.split("=") for p in path.split("?")[1].split("&")) if "?" in path else {}
            start = int(qs.get("cursor", 0))
            chunk = items[start:start + self.page_size]
            nxt = start + self.page_size
            out = {"data": chunk, "nextCursor": str(nxt) if nxt < len(items) else None}
            return 200, json.dumps(out).encode()
        if method == "POST" and route == "/workflows":
            self.next_id += 1
            wid = str(self.next_id)
            self.workflows[wid] = {"id": wid, **data}
            return 200, json.dumps({"id": wid, **data}).encode()
        if method == "PUT" and route.startswith("/workflows/"):
            wid = route.rsplit("/", 1)[1]
            if wid not in self.workflows:
                return 404, b"{}"
            self.workflows[wid] = {"id": wid, **data}
            return 200, json.dumps(self.workflows[wid]).encode()
        if method == "POST" and route.endswith(("/activate", "/deactivate")):
            return 200, b"{}"
        return 404, b"{}"

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]
