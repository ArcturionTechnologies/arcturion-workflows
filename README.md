# ArcturionWorkflows

n8n workflows as code. Describe a workflow in a small JSON file, check it into
git, and let one command create or update it in n8n.

Without something like this, an n8n canvas is a place you click until it works
and cannot reproduce. ArcturionWorkflows turns a workflow into a reviewable file:

- it **finds the workflow by name**, then **updates it in place or creates it**,
  so running it twice never makes a duplicate,
- it adds **manual, schedule and webhook triggers** to every workflow (the n8n
  API cannot start a run by itself, so the webhook is how you test),
- it **defaults to a dry run**: nothing is written until you say `--apply`,
  and nodes that send or write ship switched off while the workflow is in dry-run mode,
- it is a **template driven by environment variables**: no credential id, chat id
  or API key ever lives in a spec file.

Python 3.10+, standard library only.

> **Portfolio project.** This is an open-source sample of the tooling behind
> Arcturion's automation work. It is not a commercial product and makes no claims
> about revenue or customers. The examples use placeholder names and local addresses.

## Quickstart

```bash
git clone https://github.com/ArcturionTechnologies/arcturion-workflows.git
cd arcturion-workflows

# 1. Build the JSON offline. No n8n, no network.
python3 -m arcturion_workflows build examples/nightly-digest.json --out workflows-out

# 2. Plan against a real n8n. Read-only, writes nothing.
export N8N_API_KEY=...                 # from your own secret store
export N8N_CRED_SHIM_ID=...            # ids of credentials you created in n8n
export N8N_CRED_NOTIFY_ID=...
export N8N_NOTIFY_CHAT_ID=...
python3 -m arcturion_workflows deploy examples/nightly-digest.json
# [DRY RUN] Example Nightly Digest: create, 6 nodes
#   disabled while dry_run is on: Notify
#   nothing was written. Re-run with --apply to write.

# 3. Write it. Add --activate to switch the triggers on.
python3 -m arcturion_workflows deploy examples/nightly-digest.json --apply
```

Or install it as a command: `pip install .`, then run `arcturion-workflows ...`.

## A spec

```json
{
  "name": "Example Nightly Digest",
  "timezone": "UTC",
  "dry_run": true,
  "triggers": {
    "manual": true,
    "schedule": { "cron": "0 2 * * *" },
    "webhook": { "path": "example-nightly-digest", "method": "GET" }
  },
  "credentials": {
    "shim":   { "type": "httpHeaderAuth", "name": "Local shim (header auth)", "id_env": "N8N_CRED_SHIM_ID" },
    "notify": { "type": "telegramApi",    "name": "Notifier bot",             "id_env": "N8N_CRED_NOTIFY_ID" }
  },
  "steps": [
    { "name": "Get Status", "type": "http", "url": "http://127.0.0.1:5691/status", "credential": "shim" },
    { "name": "Render Summary", "type": "code", "js": "const DRY_RUN = __DRY_RUN__; ..." },
    { "name": "Notify", "type": "telegram", "chat_id_env": "N8N_NOTIFY_CHAT_ID",
      "text": "={{ $json.text }}", "credential": "notify" }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `name` | The key for find-by-name. Must be unique in your n8n. |
| `dry_run` | Default `true`. Disables every node that writes or sends. In `code` steps, `__DRY_RUN__` becomes `true` or `false`. |
| `triggers` | `manual`, `schedule` (5-field cron), `webhook` (path and method). Enable any combination, at least one. |
| `credentials` | A logical name, the n8n credential type and display name, and `id_env`: the **name of an environment variable** holding the credential id. Putting a literal `id` in a spec is rejected. |
| `steps` | Run in order after every trigger. Types: `http`, `code`, `telegram`. |
| `${VAR}` | In URLs and webhook paths, filled from the environment at build time. |

A step counts as "mutating" when it is a Telegram message or an HTTP POST, PUT,
PATCH or DELETE. Override with `"mutating": true|false`. For Telegram steps give
either `chat_id_env` or an n8n expression in `chat_id_expr`; a literal chat id is
rejected.

If an environment variable is unset, a plan shows it by name and the built JSON
carries a visible `<unset:NAME>` placeholder. `--apply` refuses to run until
every one is set.

## Commands

```
arcturion-workflows build  SPEC... [--out DIR]
arcturion-workflows deploy SPEC... [--apply] [--activate] [--base-url URL]
```

| Setting | Source | Default |
| --- | --- | --- |
| API key | `N8N_API_KEY` (environment only, no flag) | required to talk to n8n |
| Base URL | `--base-url` or `N8N_BASE_URL` | `http://127.0.0.1:5678` |

What `deploy` does:

1. Lists workflows (read-only) and looks for an exact name match. Two matches is
   an error, not a guess.
2. Plan mode stops here and prints what would happen. With no API key it stays
   fully offline and says it cannot tell create from update.
3. `--apply` sends `PUT` to the existing workflow or `POST` to create one.
4. `--activate` deactivates then activates, which re-registers webhook triggers,
   and prints the production webhook URL.

Exit codes: `0` ok, `2` bad spec, missing key or unset variables on `--apply`, or an n8n error.

## From a scheduled job to n8n

[`docs/launchd-to-n8n-cutover.md`](docs/launchd-to-n8n-cutover.md) is a generic
runbook for moving a launchd or cron job onto an n8n schedule safely: wrap the
logic behind a local service, build the workflow in dry run, verify through the
webhook, do one real run, then flip and retire the old scheduler in the same move.
It also lists the n8n API traps that are easy to hit.

## Project layout

```
arcturion_workflows/spec.py      spec validation, ${ENV} expansion, what counts as mutating
arcturion_workflows/builder.py   spec -> n8n payload (pure function)
arcturion_workflows/client.py    small Public API client with an injectable transport
arcturion_workflows/deploy.py    find by name, update or create, dry run by default
arcturion_workflows/cli.py       command line
examples/                        two placeholder specs
docs/launchd-to-n8n-cutover.md   generic cutover runbook
tests/                           44 tests, stdlib unittest
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Everything runs offline against an in-memory fake of the n8n API, and the suite
blocks real socket connections so a stray network call fails the test.

## Limits

- Steps run in a straight line. Branches, merges and loops are not modelled; for
  those, build the workflow in the editor and keep it as an exported JSON file.
- Credentials are referenced by id but not created. The n8n API can create and
  delete them but not list or update them, and OAuth credentials need the editor.
- Written against the n8n Public API v1 and node versions current in 2026.
  Treat node `typeVersion` values as something to check against your n8n.

## License

MIT. See [LICENSE](LICENSE).

Implementation is AI-assisted; architecture, requirements, and testing directed by Robert Lingoes.
