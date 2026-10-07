"""Compile a spec into an n8n workflow payload (pure function: no I/O, no network)."""
import json
import os
import uuid

from .spec import expand, is_mutating, validate_spec

NS = uuid.UUID("6f0f1d4e-7a64-4f43-9c7e-2f1d0d2a6b11")  # fixed namespace for deterministic ids
HTTP_AUTH = {"httpHeaderAuth": "httpHeaderAuth", "httpBasicAuth": "httpBasicAuth"}


class Build:
    def __init__(self, payload, missing_env, disabled, webhook_paths, dry_run):
        self.payload = payload
        self.missing_env = sorted(missing_env)
        self.disabled = disabled
        self.webhook_paths = webhook_paths
        self.dry_run = dry_run

    @property
    def name(self):
        return self.payload["name"]


def webhook_id(workflow_name, path):
    """Deterministic UUID. API-created webhook nodes need a webhookId or activation silently
    skips registration; deriving it from name+path keeps rebuilds idempotent."""
    return str(uuid.uuid5(NS, f"{workflow_name}:{path}"))


def _cred_ref(spec, key, env, missing):
    c = spec["credentials"][key]
    cid = env.get(c["id_env"])
    if not cid:
        missing.add(c["id_env"])
        cid = f"<unset:{c['id_env']}>"
    return c["type"], {"id": cid, "name": c["name"]}


def build_workflow(spec, env=None):
    env = os.environ if env is None else env
    validate_spec(spec)
    dry_run = spec.get("dry_run", True)
    missing, disabled = set(), []
    name = spec["name"]
    triggers = spec["triggers"]

    nodes, trigger_names = [], []
    y = -160

    def add_trigger(node):
        nonlocal y
        node["position"] = [-380, y]
        y += 160
        nodes.append(node)
        trigger_names.append(node["name"])

    if triggers.get("manual"):
        add_trigger({"id": "manual", "name": "Manual Trigger", "type": "n8n-nodes-base.manualTrigger",
                     "typeVersion": 1, "parameters": {}})
    if triggers.get("schedule"):
        add_trigger({"id": "schedule", "name": "Schedule", "type": "n8n-nodes-base.scheduleTrigger",
                     "typeVersion": 1.2, "parameters": {"rule": {"interval": [
                         {"field": "cronExpression", "expression": triggers["schedule"]["cron"]}]}}})
    webhook_paths = []
    if triggers.get("webhook"):
        hook = triggers["webhook"]
        path = expand(hook["path"], env, missing)
        webhook_paths.append(path)
        add_trigger({"id": "webhook", "name": "Webhook (on-demand)", "type": "n8n-nodes-base.webhook",
                     "typeVersion": 2, "webhookId": webhook_id(name, path),
                     "parameters": {"httpMethod": hook.get("method", "GET").upper(), "path": path,
                                    "responseMode": "onReceived", "options": {}}})

    prev = None
    connections = {}
    for i, s in enumerate(spec["steps"]):
        node = _step_node(s, i, spec, env, missing, dry_run)
        node["position"] = [-140 + 220 * i, 0]
        if node.get("disabled"):
            disabled.append(node["name"])
        nodes.append(node)
        if prev is None:
            for t in trigger_names:
                connections[t] = {"main": [[{"node": node["name"], "type": "main", "index": 0}]]}
        else:
            connections[prev] = {"main": [[{"node": node["name"], "type": "main", "index": 0}]]}
        prev = node["name"]

    payload = {"name": name, "nodes": nodes, "connections": connections,
               "settings": {"executionOrder": "v1", "timezone": spec.get("timezone", "UTC")}}
    return Build(payload, missing, disabled, webhook_paths, dry_run)


def _step_node(s, i, spec, env, missing, dry_run):
    node = {"id": s.get("id") or f"step{i + 1}", "name": s["name"], "parameters": {}}
    kind = s["type"]
    if kind == "http":
        node.update(type="n8n-nodes-base.httpRequest", typeVersion=4.2)
        method = s.get("method", "GET").upper()
        p = {"url": expand(s["url"], env, missing), "options": {"timeout": int(s.get("timeout_ms", 30000))}}
        if method != "GET":
            p["method"] = method
        if s.get("json_body"):
            p.update(sendBody=True, specifyBody="json", jsonBody=s["json_body"])
        node["parameters"] = p
        if s.get("credential"):
            ctype, ref = _cred_ref(spec, s["credential"], env, missing)
            p.update(authentication="genericCredentialType", genericAuthType=ctype)
            node["credentials"] = {ctype: ref}
    elif kind == "code":
        js = s["js"].replace("__DRY_RUN__", "true" if dry_run else "false")
        node.update(type="n8n-nodes-base.code", typeVersion=2, parameters={"jsCode": js})
    elif kind == "telegram":
        node.update(type="n8n-nodes-base.telegram", typeVersion=1.2)
        chat = s["chat_id_expr"] if s.get("chat_id_expr") else expand("${%s}" % s["chat_id_env"], env, missing)
        node["parameters"] = {"chatId": chat, "text": s["text"],
                              "additionalFields": {"appendAttribution": False}}
        ctype, ref = _cred_ref(spec, s["credential"], env, missing)
        node["credentials"] = {ctype: ref}
    if dry_run and is_mutating(s):
        node["disabled"] = True
    return node


def to_json(build):
    return json.dumps(build.payload, indent=2, ensure_ascii=False) + "\n"
