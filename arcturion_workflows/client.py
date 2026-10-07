"""Minimal n8n Public API client. The transport is injectable so tests never use a network."""
import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:5678"


class N8nError(RuntimeError):
    pass


def urllib_transport(method, url, headers, body, timeout):
    """Return (status, response_bytes). HTTP errors return their status instead of raising."""
    req = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        finally:
            e.close()
    except urllib.error.URLError as e:
        raise N8nError(f"cannot reach n8n at {url}: {e.reason}")


class N8nClient:
    def __init__(self, base_url=None, api_key=None, transport=urllib_transport, timeout=30):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api = self.base_url + "/api/v1"
        self.api_key = api_key
        self.transport = transport
        self.timeout = timeout

    @classmethod
    def from_env(cls, env=None, base_url=None, transport=urllib_transport):
        env = os.environ if env is None else env
        return cls(base_url or env.get("N8N_BASE_URL"), env.get("N8N_API_KEY"), transport)

    @property
    def has_key(self):
        return bool(self.api_key)

    def request(self, method, path, body=None):
        if not self.api_key:
            raise N8nError("N8N_API_KEY is not set")
        headers = {"X-N8N-API-KEY": self.api_key, "Content-Type": "application/json",
                   "Accept": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        status, raw = self.transport(method, self.api + path, headers, data, self.timeout)
        if not 200 <= status < 300:
            snippet = raw[:300].decode("utf-8", "replace") if raw else ""
            raise N8nError(f"HTTP {status} on {method} {path}: {snippet}")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise N8nError(f"non-JSON response on {method} {path}")

    # ---- workflows
    def list_workflows(self):
        out, cursor = [], None
        for _ in range(1000):  # hard stop against a cursor that never ends
            path = "/workflows?limit=100" + (f"&cursor={cursor}" if cursor else "")
            page = self.request("GET", path)
            out.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return out
        raise N8nError("workflow listing did not terminate")

    def find_by_name(self, name):
        """Return the one workflow with exactly this name, or None. More than one is an error."""
        matches = [w for w in self.list_workflows() if w.get("name") == name]
        if len(matches) > 1:
            ids = ", ".join(str(w.get("id")) for w in matches)
            raise N8nError(f"{len(matches)} workflows are named {name!r} (ids: {ids}); rename or delete duplicates")
        return matches[0] if matches else None

    def create(self, payload):
        return self.request("POST", "/workflows", payload)

    def update(self, workflow_id, payload):
        return self.request("PUT", f"/workflows/{workflow_id}", payload)

    def activate(self, workflow_id):
        return self.request("POST", f"/workflows/{workflow_id}/activate", {})

    def deactivate(self, workflow_id):
        return self.request("POST", f"/workflows/{workflow_id}/deactivate", {})
