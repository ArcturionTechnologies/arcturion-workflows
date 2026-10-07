# Runbook: moving a scheduled job from launchd (or cron) to n8n

This is the checklist for retiring a scheduled job and letting an n8n workflow
own the schedule instead. It is written for a macOS launchd job, but the same
steps apply to cron or a systemd timer.

The goal is simple: **at any moment exactly one thing fires the job, and what
you see on the n8n canvas is what actually runs.**

Examples below use a made-up job called `com.example.nightly-report`. Replace
it with your own label.

## Principles

1. **The canvas is the truth.** When a job moves to n8n, its old scheduler entry
   is retired in the same move. Never leave both running.
2. **n8n does not run shell commands.** Recent n8n versions disable the Execute
   Command node by default, and you should leave it that way. Put the existing
   logic behind a small local HTTP service (a "shim") that wraps the function you
   already have. n8n calls the shim. The output is identical by construction,
   because it is the same code.
3. **Dry run is the default.** Anything that writes, sends or deletes ships
   switched off, and is only enabled at the cutover step after a verified dry run.
4. **Wrap, don't rewrite.** The cutover is about who owns the schedule, not about
   changing what the job does. Keep those two changes in separate steps.

## What you need first

- n8n reachable on a local address, with API access enabled and a key in the
  `N8N_API_KEY` environment variable (never in a file in the repo).
- The job's current schedule, command and last good output. Write them down.
- A shim for the job: a localhost-only service with a bearer token. The token is
  an environment variable on the machine that runs the shim. n8n stores its own
  copy as an *HTTP Header Auth* credential.
- A workflow spec for the job (see `examples/nightly-digest.json`).

## Checklist

### 1. Shim the logic

Expose the job's existing function on a localhost endpoint, protected by a bearer
token. The endpoint takes a `dry_run` flag that defaults to `true`; in dry-run
mode it computes everything and writes nothing.

*Done when:* calling the endpoint by hand with `dry_run: true` returns the result
and leaves no files, messages or database rows behind.

### 2. Build the workflow with all three triggers, dry run on

Write a spec with a manual, a schedule and a webhook trigger. Give it the same
cron expression as the old job so the timing matches.

```bash
export N8N_API_KEY=...            # from your own secret store
export N8N_CRED_SHIM_ID=...       # id of the header-auth credential in n8n
python3 -m arcturion_workflows deploy examples/nightly-digest.json            # plan only
python3 -m arcturion_workflows deploy examples/nightly-digest.json --apply
```

The first command reports whether the workflow would be created or updated, and
which nodes are disabled by dry run. The second writes it. Leave it inactive for
now.

*Done when:* the workflow exists in n8n, is inactive, and every node that writes
or sends is shown as disabled.

### 3. Fire it through the webhook and read the execution

Activate it (`--apply --activate`), call the webhook URL, and open the execution
in n8n with full data. Check each node's input and output.

*Done when:* the execution is green end to end and the data at each node matches
what the old job produced.

### 4. One real run

Turn dry run off in the spec (`"dry_run": false`), deploy, and trigger one real
run by hand. Verify the real artifact: the file on disk, the message received,
the row written.

*Done when:* the artifact is correct and appears exactly once.

### 5. Flip and retire in the same move

In one sitting:

1. Confirm the n8n workflow is active with `dry_run: false`.
2. Retire the old scheduler entry:
   ```bash
   launchctl bootout gui/$(id -u)/com.example.nightly-report
   mv ~/Library/LaunchAgents/com.example.nightly-report.plist ~/launchd-archive/
   ```
3. Confirm nothing else is still loaded under that label:
   `launchctl list | grep nightly-report` should print nothing.

Archive the old definition rather than deleting it. It is your rollback.

### 6. Confirm the next scheduled fire comes from n8n only

Wait for the next scheduled time. Check that n8n shows one scheduled execution
and that the old scheduler produced none. If you see two runs, stop and
fix it before doing anything else.

### 7. Record what changed

Write down: the job, the old schedule, the new workflow name, the date, and where
the archived definition lives. If you keep a changelog or runbook index, update it.

## Rollback

If the n8n run misbehaves after cutover:

1. Deactivate the workflow in n8n (or `POST /workflows/{id}/deactivate`).
2. Restore the archived definition and load it
   (`launchctl bootstrap gui/$(id -u) <plist>`).
3. Confirm only one scheduler is active again.

## Things that will cost you an afternoon

- **The Public API cannot start a manual run.** That is why every workflow has a
  webhook trigger: it is how you test.
- **API-created webhook nodes need a `webhookId`**, or activation silently skips
  registering them and the URL returns 404. ArcturionWorkflows derives a stable
  id from the workflow name and path. After an update, deactivate then activate
  to re-register (`--activate` does both).
- **Credentials cannot be listed or updated through the API**, only created and
  deleted. To change one, delete it, recreate it, and point the nodes at the new
  id. OAuth-based credentials (Google and similar) generally have to be created
  in the n8n editor because they need a consent click.
- **Merge nodes:** put `numberInputs` inside `parameters`, and use *append*
  mode. *Combine all* cross-joins and can drop keys that collide.
- **Reference upstream nodes by name** in Code nodes
  (`$('Node Name').all()`), never by position.
- **Build request bodies in a Code node** and send `JSON.stringify($json)`
  rather than writing large object literals inline.
- **Time zones:** set the workflow time zone explicitly. A schedule is
  interpreted in the workflow's zone, not the machine's.
- **Duplicate names:** if two workflows share a name, "find by name" cannot know
  which you mean. ArcturionWorkflows refuses and lists the ids.

## Evidence to keep for each cutover

| Step | Evidence |
| --- | --- |
| 1 | Hand call with `dry_run: true` and an empty diff of the output folder |
| 3 | Link to the green execution |
| 4 | The real artifact and the single execution that made it |
| 5 | `launchctl list` output with the old label gone |
| 6 | The next scheduled execution in n8n and no run from the old scheduler |
