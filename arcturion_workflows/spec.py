"""Workflow spec: a small JSON description that is compiled into an n8n workflow.

    {
      "name": "Example Nightly Digest",
      "timezone": "UTC",
      "dry_run": true,                      // default true
      "triggers": {
        "manual": true,
        "schedule": {"cron": "0 2 * * *"},
        "webhook": {"path": "example-nightly-digest", "method": "GET"}
      },
      "credentials": {                      // logical name -> n8n credential, id from the environment
        "shim": {"type": "httpHeaderAuth", "name": "Local shim", "id_env": "N8N_CRED_SHIM_ID"}
      },
      "steps": [ ...run in order after every trigger... ]
    }

Step types: `http`, `code`, `telegram`. Strings may contain ${ENV_VAR}; they are filled in
from the environment when the workflow is built. Nothing identifying is ever stored in a spec:
credential ids and chat ids are only accepted as environment variable names.
"""
import re

STEP_TYPES = {"http", "code", "telegram"}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
WEBHOOK_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-/]*$")
CRON_FIELDS = 5


class SpecError(ValueError):
    pass


def _require(cond, msg):
    if not cond:
        raise SpecError(msg)


def validate_spec(spec):
    """Raise SpecError for anything malformed. Returns the spec."""
    _require(isinstance(spec, dict), "spec must be a JSON object")
    _require(isinstance(spec.get("name"), str) and spec["name"].strip(), "spec needs a non-empty 'name'")
    if "dry_run" in spec:
        _require(isinstance(spec["dry_run"], bool), "'dry_run' must be true or false")

    triggers = spec.get("triggers")
    _require(isinstance(triggers, dict), "spec needs a 'triggers' object")
    unknown = set(triggers) - {"manual", "schedule", "webhook"}
    _require(not unknown, f"unknown trigger(s): {sorted(unknown)}")
    _require(any(triggers.get(k) for k in ("manual", "schedule", "webhook")),
             "enable at least one trigger (manual, schedule, webhook)")
    sched = triggers.get("schedule")
    if sched:
        cron = sched.get("cron") if isinstance(sched, dict) else None
        _require(isinstance(cron, str) and len(cron.split()) == CRON_FIELDS,
                 "schedule.cron must be a 5-field cron expression")
    hook = triggers.get("webhook")
    if hook:
        _require(isinstance(hook, dict) and WEBHOOK_PATH.match(str(hook.get("path", ""))),
                 "webhook.path is required (letters, digits, '-', '_', '/')")
        _require(hook.get("method", "GET").upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"},
                 "webhook.method must be a standard HTTP method")

    creds = spec.get("credentials", {})
    _require(isinstance(creds, dict), "'credentials' must be an object")
    for key, c in creds.items():
        _require(isinstance(c, dict), f"credentials.{key} must be an object")
        _require("id" not in c, f"credentials.{key}: do not put an id in the spec; use 'id_env' with an environment variable name")
        _require(c.get("type") and c.get("name") and c.get("id_env"),
                 f"credentials.{key} needs 'type', 'name' and 'id_env'")

    steps = spec.get("steps")
    _require(isinstance(steps, list) and steps, "spec needs a non-empty 'steps' list")
    names = set()
    for i, s in enumerate(steps):
        where = f"steps[{i}]"
        _require(isinstance(s, dict), f"{where} must be an object")
        _require(s.get("type") in STEP_TYPES, f"{where}: type must be one of {sorted(STEP_TYPES)}")
        _require(isinstance(s.get("name"), str) and s["name"].strip(), f"{where}: needs a 'name'")
        _require(s["name"] not in names, f"{where}: duplicate step name {s['name']!r}")
        names.add(s["name"])
        if s.get("credential"):
            _require(s["credential"] in creds, f"{where}: credential {s['credential']!r} is not defined in 'credentials'")
        if s["type"] == "http":
            _require(isinstance(s.get("url"), str) and s["url"], f"{where}: http step needs 'url'")
            _require(s.get("method", "GET").upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}, f"{where}: bad method")
        elif s["type"] == "code":
            _require(isinstance(s.get("js"), str) and s["js"].strip(), f"{where}: code step needs 'js'")
        elif s["type"] == "telegram":
            _require(bool(s.get("chat_id_env")) != bool(s.get("chat_id_expr")),
                     f"{where}: telegram step needs exactly one of 'chat_id_env' or 'chat_id_expr'")
            if s.get("chat_id_expr"):
                _require(str(s["chat_id_expr"]).startswith("="), f"{where}: chat_id_expr must be an n8n expression starting with '='")
            _require(s.get("credential"), f"{where}: telegram step needs a 'credential'")
            _require(isinstance(s.get("text"), str) and s["text"], f"{where}: telegram step needs 'text'")
    return spec


def is_mutating(step):
    """Steps that change the outside world. They ship disabled while dry_run is on."""
    if "mutating" in step:
        return bool(step["mutating"])
    if step["type"] == "telegram":
        return True
    if step["type"] == "http":
        return step.get("method", "GET").upper() in MUTATING_METHODS
    return False


def expand(value, env, missing):
    """Replace ${VAR} in a string from `env`; record unset names in `missing`."""
    if not isinstance(value, str):
        return value

    def sub(m):
        name = m.group(1)
        if env.get(name):
            return env[name]
        missing.add(name)
        return f"<unset:{name}>"
    return ENV_REF.sub(sub, value)
