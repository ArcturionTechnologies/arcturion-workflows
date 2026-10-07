"""Command line: build workflow JSON offline, or deploy it to n8n (dry run by default)."""
import argparse
import json
import os
import sys

from .builder import build_workflow, to_json
from .client import N8nClient, N8nError
from .deploy import DeployError, deploy
from .spec import SpecError


def load_spec(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        raise SpecError(f"cannot read {path}: {e.strerror}")
    except json.JSONDecodeError as e:
        raise SpecError(f"{path} is not valid JSON: {e}")


def _slug(name):
    out = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "workflow"


def cmd_build(args, env, out):
    os.makedirs(args.out, exist_ok=True)
    for path in args.specs:
        b = build_workflow(load_spec(path), env)
        target = os.path.join(args.out, _slug(b.name) + ".workflow.json")
        with open(target, "w", encoding="utf-8") as f:
            f.write(to_json(b))
        print(f"built {b.name!r} -> {target} ({len(b.payload['nodes'])} nodes)", file=out)
        if b.missing_env:
            print("  note: unset environment variables left as placeholders: " + ", ".join(b.missing_env), file=out)
    return 0


def cmd_deploy(args, env, out, client=None):
    client = client or N8nClient.from_env(env, base_url=args.base_url)
    rc = 0
    for path in args.specs:
        r = deploy(load_spec(path), client, env, apply=args.apply, activate=args.activate)
        label = "APPLIED" if r["applied"] else "DRY RUN"
        print(f"[{label}] {r['name']}: {r['action']}"
              + (f" (id {r['id']})" if r.get("id") else "") + f", {r['nodes']} nodes", file=out)
        if r["disabled_nodes"]:
            print("  disabled while dry_run is on: " + ", ".join(r["disabled_nodes"]), file=out)
        if r["missing_env"]:
            print("  unset environment variables: " + ", ".join(r["missing_env"]), file=out)
        if r.get("lookup_error"):
            print("  lookup failed: " + r["lookup_error"], file=out)
        if r.get("activated"):
            print("  activated; webhook: " + ", ".join(r["webhook_urls"] or ["(none)"]), file=out)
        if not r["applied"]:
            print("  nothing was written. Re-run with --apply to write.", file=out)
    return rc


def build_parser():
    ap = argparse.ArgumentParser(prog="arcturion-workflows", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="compile specs to n8n workflow JSON files (offline)")
    b.add_argument("specs", nargs="+")
    b.add_argument("--out", default="workflows-out")

    d = sub.add_parser("deploy", help="find by name, then update or create. Dry run unless --apply")
    d.add_argument("specs", nargs="+")
    d.add_argument("--apply", action="store_true", help="actually write to n8n (default is a dry run)")
    d.add_argument("--activate", action="store_true", help="after writing, deactivate then activate to re-register triggers")
    d.add_argument("--base-url", default=None, help="default: N8N_BASE_URL or http://127.0.0.1:5678")
    return ap


def main(argv=None, env=None, out=None, client=None):
    env = os.environ if env is None else env
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "build":
            return cmd_build(args, env, out)
        return cmd_deploy(args, env, out, client)
    except (SpecError, DeployError, N8nError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
