"""Find a workflow by name, then update or create it. Dry run unless apply=True."""
from .builder import build_workflow
from .client import N8nError


class DeployError(RuntimeError):
    pass


def _summary(build):
    return {"name": build.name, "nodes": len(build.payload["nodes"]),
            "dry_run_workflow": build.dry_run, "disabled_nodes": build.disabled,
            "missing_env": build.missing_env, "webhook_paths": build.webhook_paths}


def deploy(spec, client, env=None, apply=False, activate=False):
    """Plan or perform one deployment. Returns a result dict.

    Without `apply` nothing is written. If the client has an API key the plan does one
    read-only listing to say whether the workflow would be created or updated; without
    a key it says so and stays fully offline.
    `activate` re-registers triggers: deactivate (ignored if it fails) then activate.
    """
    build = build_workflow(spec, env)
    result = _summary(build)
    result["applied"] = False

    existing = None
    if client.has_key:
        try:
            existing = client.find_by_name(build.name)
        except N8nError as e:
            if apply:
                raise
            result["lookup_error"] = str(e)
            result["action"] = "unknown"
            return result
        result["action"] = "update" if existing else "create"
        if existing:
            result["id"] = existing["id"]
    else:
        if apply:
            raise DeployError("N8N_API_KEY is not set; refusing to apply")
        result["action"] = "unknown (no API key; offline plan)"

    if not apply:
        return result

    if build.missing_env:
        raise DeployError("refusing to apply with unset environment variables: " + ", ".join(build.missing_env))

    if existing:
        wf = client.update(existing["id"], build.payload)
        wid = wf.get("id", existing["id"])
    else:
        wf = client.create(build.payload)
        wid = wf["id"]
    result.update(applied=True, id=wid)

    if activate:
        try:
            client.deactivate(wid)
        except N8nError:
            pass
        client.activate(wid)
        result["activated"] = True
        result["webhook_urls"] = [f"{client.base_url}/webhook/{p}" for p in build.webhook_paths]
    return result
