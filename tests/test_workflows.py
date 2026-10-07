import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import FakeN8n, OfflineTestCase, REPO
from arcturion_workflows import builder, cli, deploy as deploy_mod
from arcturion_workflows.client import N8nClient, N8nError
from arcturion_workflows.spec import SpecError, expand, is_mutating, validate_spec

ENV = {"N8N_CRED_SHIM_ID": "cred-shim-1", "N8N_CRED_NOTIFY_ID": "cred-notify-1",
       "N8N_NOTIFY_CHAT_ID": "chat-test-1", "N8N_API_KEY": "test-key"}


def example(name="nightly-digest"):
    return json.loads((REPO / "examples" / f"{name}.json").read_text())


def client(fake, key="test-key"):
    return N8nClient("http://n8n.example.test:5678", key, transport=fake)


def node(build, name):
    return [n for n in build.payload["nodes"] if n["name"] == name][0]


class SpecValidation(OfflineTestCase):
    def bad(self, mutate, fragment):
        spec = example()
        mutate(spec)
        with self.assertRaises(SpecError) as cm:
            validate_spec(spec)
        self.assertIn(fragment, str(cm.exception))

    def test_examples_validate(self):
        validate_spec(example())
        validate_spec(example("webhook-only"))

    def test_requires_name_triggers_steps(self):
        self.bad(lambda s: s.pop("name"), "name")
        self.bad(lambda s: s.update(triggers={}), "at least one trigger")
        self.bad(lambda s: s.update(steps=[]), "steps")

    def test_unknown_trigger_and_bad_cron_and_bad_webhook_path(self):
        self.bad(lambda s: s["triggers"].update(carrier_pigeon=True), "unknown trigger")
        self.bad(lambda s: s["triggers"].update(schedule={"cron": "0 2 *"}), "5-field")
        self.bad(lambda s: s["triggers"].update(webhook={"path": "bad path!"}), "webhook.path")

    def test_credential_ids_may_not_be_literal(self):
        self.bad(lambda s: s["credentials"]["shim"].update(id="abc123"), "do not put an id")

    def test_step_rules(self):
        self.bad(lambda s: s["steps"][0].update(credential="nope"), "not defined")
        self.bad(lambda s: s["steps"][0].update(type="shell"), "type must be")
        self.bad(lambda s: s["steps"].append(dict(s["steps"][0])), "duplicate step name")
        self.bad(lambda s: s["steps"][1].pop("js"), "needs 'js'")
        self.bad(lambda s: s["steps"][0].pop("url"), "needs 'url'")

    def test_telegram_chat_must_come_from_env_or_expression(self):
        def lit(s):
            s["steps"][2].pop("chat_id_env")
        self.bad(lit, "exactly one of")

        def both(s):
            s["steps"][2]["chat_id_expr"] = "={{ $json.chat }}"
        self.bad(both, "exactly one of")

        def not_expr(s):
            s["steps"][2].pop("chat_id_env")
            s["steps"][2]["chat_id_expr"] = "12345"
        self.bad(not_expr, "expression starting with '='")

    def test_expand_and_mutating(self):
        miss = set()
        self.assertEqual(expand("a-${X}-${Y}", {"X": "1"}, miss), "a-1-<unset:Y>")
        self.assertEqual(miss, {"Y"})
        self.assertFalse(is_mutating({"type": "http", "method": "GET"}))
        self.assertTrue(is_mutating({"type": "http", "method": "post"}))
        self.assertFalse(is_mutating({"type": "http", "method": "POST", "mutating": False}))
        self.assertFalse(is_mutating({"type": "code"}))
        self.assertTrue(is_mutating({"type": "telegram"}))


class Builder(OfflineTestCase):
    def test_all_three_triggers_feed_the_first_step(self):
        b = builder.build_workflow(example(), ENV)
        types = {n["type"] for n in b.payload["nodes"]}
        self.assertTrue({"n8n-nodes-base.manualTrigger", "n8n-nodes-base.scheduleTrigger",
                         "n8n-nodes-base.webhook"} <= types)
        for t in ("Manual Trigger", "Schedule", "Webhook (on-demand)"):
            target = b.payload["connections"][t]["main"][0][0]["node"]
            self.assertEqual(target, "Get Status")
        self.assertEqual(b.payload["connections"]["Get Status"]["main"][0][0]["node"], "Render Summary")
        self.assertNotIn("Notify", b.payload["connections"])

    def test_schedule_cron_and_timezone(self):
        b = builder.build_workflow(example(), ENV)
        rule = node(b, "Schedule")["parameters"]["rule"]["interval"][0]
        self.assertEqual(rule, {"field": "cronExpression", "expression": "0 2 * * *"})
        self.assertEqual(b.payload["settings"], {"executionOrder": "v1", "timezone": "UTC"})

    def test_webhook_id_is_set_and_stable(self):
        a = node(builder.build_workflow(example(), ENV), "Webhook (on-demand)")
        b = node(builder.build_workflow(example(), {}), "Webhook (on-demand)")
        self.assertTrue(a["webhookId"])
        self.assertEqual(a["webhookId"], b["webhookId"])
        other = example()
        other["name"] = "Another"
        self.assertNotEqual(a["webhookId"], node(builder.build_workflow(other, ENV), "Webhook (on-demand)")["webhookId"])
        self.assertEqual(a["parameters"]["path"], "example-nightly-digest")

    def test_dry_run_is_the_default_and_disables_mutating_nodes(self):
        spec = example()
        spec.pop("dry_run")
        b = builder.build_workflow(spec, ENV)
        self.assertTrue(b.dry_run)
        self.assertEqual(b.disabled, ["Notify"])
        self.assertTrue(node(b, "Notify")["disabled"])
        self.assertNotIn("disabled", node(b, "Get Status"))
        self.assertIn("const DRY_RUN = true;", node(b, "Render Summary")["parameters"]["jsCode"])

    def test_dry_run_false_enables_everything(self):
        spec = example()
        spec["dry_run"] = False
        b = builder.build_workflow(spec, ENV)
        self.assertEqual(b.disabled, [])
        self.assertNotIn("disabled", node(b, "Notify"))
        self.assertIn("const DRY_RUN = false;", node(b, "Render Summary")["parameters"]["jsCode"])

    def test_credentials_and_chat_id_come_from_env_only(self):
        b = builder.build_workflow(example(), ENV)
        http = node(b, "Get Status")
        self.assertEqual(http["credentials"]["httpHeaderAuth"], {"id": "cred-shim-1", "name": "Local shim (header auth)"})
        self.assertEqual(http["parameters"]["authentication"], "genericCredentialType")
        tg = node(b, "Notify")
        self.assertEqual(tg["credentials"]["telegramApi"]["id"], "cred-notify-1")
        self.assertEqual(tg["parameters"]["chatId"], "chat-test-1")
        self.assertEqual(b.missing_env, [])

    def test_missing_env_becomes_visible_placeholders(self):
        b = builder.build_workflow(example(), {})
        self.assertEqual(b.missing_env, ["N8N_CRED_NOTIFY_ID", "N8N_CRED_SHIM_ID", "N8N_NOTIFY_CHAT_ID"])
        self.assertEqual(node(b, "Notify")["parameters"]["chatId"], "<unset:N8N_NOTIFY_CHAT_ID>")

    def test_url_expansion_and_http_body(self):
        b = builder.build_workflow(example("webhook-only"), {"LOCAL_SERVICE_URL": "http://127.0.0.1:9000"})
        fwd = node(b, "Forward To Local Service")
        self.assertEqual(fwd["parameters"]["url"], "http://127.0.0.1:9000/ingest")
        self.assertEqual(fwd["parameters"]["method"], "POST")
        self.assertEqual(fwd["parameters"]["jsonBody"], "={{ JSON.stringify($json) }}")
        self.assertTrue(fwd["disabled"])  # POST is mutating

    def test_payload_has_only_fields_the_api_accepts(self):
        b = builder.build_workflow(example(), ENV)
        self.assertEqual(set(b.payload), {"name", "nodes", "connections", "settings"})

    def test_to_json_round_trips(self):
        b = builder.build_workflow(example(), ENV)
        self.assertEqual(json.loads(builder.to_json(b)), b.payload)


class Client(OfflineTestCase):
    def test_sends_api_key_header_and_never_without_one(self):
        fake = FakeN8n()
        client(fake).list_workflows()
        self.assertEqual(fake.last_headers["X-N8N-API-KEY"], "test-key")
        with self.assertRaises(N8nError):
            client(fake, key=None).list_workflows()

    def test_pagination(self):
        fake = FakeN8n([{"id": str(i), "name": f"wf{i}"} for i in range(5)], page_size=2)
        names = [w["name"] for w in client(fake).list_workflows()]
        self.assertEqual(names, [f"wf{i}" for i in range(5)])
        self.assertEqual(len([c for c in fake.calls if c[0] == "GET"]), 3)

    def test_find_by_name_exact_match_only(self):
        fake = FakeN8n([{"id": "1", "name": "Example Nightly Digest"}, {"id": "2", "name": "Example Nightly Digest 2"}])
        self.assertEqual(client(fake).find_by_name("Example Nightly Digest")["id"], "1")
        self.assertIsNone(client(fake).find_by_name("example nightly digest"))

    def test_duplicate_names_are_refused(self):
        fake = FakeN8n([{"id": "1", "name": "X"}, {"id": "2", "name": "X"}])
        with self.assertRaises(N8nError) as cm:
            client(fake).find_by_name("X")
        self.assertIn("1, 2", str(cm.exception))

    def test_http_error_becomes_n8n_error(self):
        fake = FakeN8n(fail={("GET", "/workflows"): 401})
        with self.assertRaises(N8nError) as cm:
            client(fake).list_workflows()
        self.assertIn("HTTP 401", str(cm.exception))

    def test_from_env_reads_key_and_base_url(self):
        c = N8nClient.from_env({"N8N_API_KEY": "k", "N8N_BASE_URL": "http://n8n.example.test:5678/"})
        self.assertEqual((c.api_key, c.api), ("k", "http://n8n.example.test:5678/api/v1"))
        self.assertEqual(N8nClient.from_env({}).base_url, "http://127.0.0.1:5678")


class Deploy(OfflineTestCase):
    def test_dry_run_with_key_reads_but_never_writes(self):
        fake = FakeN8n()
        r = deploy_mod.deploy(example(), client(fake), ENV)
        self.assertEqual((r["action"], r["applied"]), ("create", False))
        self.assertEqual(fake.writes(), [])

    def test_dry_run_reports_update_when_name_exists(self):
        fake = FakeN8n([{"id": "7", "name": "Example Nightly Digest"}])
        r = deploy_mod.deploy(example(), client(fake), ENV)
        self.assertEqual((r["action"], r["id"]), ("update", "7"))
        self.assertEqual(fake.writes(), [])

    def test_dry_run_without_key_is_fully_offline(self):
        fake = FakeN8n()
        r = deploy_mod.deploy(example(), client(fake, key=None), {})
        self.assertIn("offline", r["action"])
        self.assertEqual(fake.calls, [])
        self.assertIn("N8N_CRED_SHIM_ID", r["missing_env"])

    def test_dry_run_survives_lookup_failure(self):
        fake = FakeN8n(fail={("GET", "/workflows"): 500})
        r = deploy_mod.deploy(example(), client(fake), ENV)
        self.assertEqual(r["action"], "unknown")
        self.assertIn("HTTP 500", r["lookup_error"])

    def test_apply_creates_when_absent(self):
        fake = FakeN8n()
        r = deploy_mod.deploy(example(), client(fake), ENV, apply=True)
        self.assertTrue(r["applied"])
        self.assertEqual([c[0] for c in fake.writes()], ["POST"])
        self.assertEqual(fake.writes()[0][1], "/workflows")
        self.assertEqual(len(fake.workflows), 1)

    def test_apply_updates_in_place_when_present(self):
        fake = FakeN8n([{"id": "7", "name": "Example Nightly Digest"}])
        r = deploy_mod.deploy(example(), client(fake), ENV, apply=True)
        self.assertEqual(r["id"], "7")
        self.assertEqual([(c[0], c[1]) for c in fake.writes()], [("PUT", "/workflows/7")])
        self.assertEqual(len(fake.workflows), 1)
        self.assertEqual(len(fake.workflows["7"]["nodes"]), 6)

    def test_apply_twice_is_idempotent(self):
        fake = FakeN8n()
        deploy_mod.deploy(example(), client(fake), ENV, apply=True)
        deploy_mod.deploy(example(), client(fake), ENV, apply=True)
        self.assertEqual(len(fake.workflows), 1)
        self.assertEqual([c[0] for c in fake.writes()], ["POST", "PUT"])

    def test_apply_refuses_unset_env_and_missing_key(self):
        fake = FakeN8n()
        with self.assertRaises(deploy_mod.DeployError):
            deploy_mod.deploy(example(), client(fake), {}, apply=True)
        self.assertEqual(fake.writes(), [])
        with self.assertRaises(deploy_mod.DeployError):
            deploy_mod.deploy(example(), client(fake, key=None), ENV, apply=True)

    def test_apply_refuses_duplicates(self):
        fake = FakeN8n([{"id": "1", "name": "Example Nightly Digest"}, {"id": "2", "name": "Example Nightly Digest"}])
        with self.assertRaises(N8nError):
            deploy_mod.deploy(example(), client(fake), ENV, apply=True)
        self.assertEqual(fake.writes(), [])

    def test_activate_deactivates_then_activates_and_reports_webhook(self):
        fake = FakeN8n()
        r = deploy_mod.deploy(example(), client(fake), ENV, apply=True, activate=True)
        paths = [c[1] for c in fake.writes()]
        wid = r["id"]
        self.assertEqual(paths[-2:], [f"/workflows/{wid}/deactivate", f"/workflows/{wid}/activate"])
        self.assertEqual(r["webhook_urls"], ["http://n8n.example.test:5678/webhook/example-nightly-digest"])

    def test_activate_tolerates_deactivate_failure(self):
        fake = FakeN8n(fail={("POST", "/workflows/1001/deactivate"): 400})
        r = deploy_mod.deploy(example(), client(fake), ENV, apply=True, activate=True)
        self.assertTrue(r["activated"])

    def test_not_activated_unless_asked(self):
        fake = FakeN8n()
        deploy_mod.deploy(example(), client(fake), ENV, apply=True)
        self.assertFalse([c for c in fake.calls if "activate" in c[1]])

    def test_pushed_payload_never_contains_unset_placeholders(self):
        fake = FakeN8n()
        deploy_mod.deploy(example(), client(fake), ENV, apply=True)
        self.assertNotIn("<unset", json.dumps(fake.writes()[0][2]))


class Cli(OfflineTestCase):
    def run_cli(self, argv, env=ENV, fake=None):
        out = io.StringIO()
        fake = fake or FakeN8n()
        c = client(fake, key=env.get("N8N_API_KEY"))
        rc = cli.main(argv, env=env, out=out, client=c)
        return rc, out.getvalue(), fake

    def spec_path(self, name="nightly-digest"):
        return str(REPO / "examples" / f"{name}.json")

    def test_deploy_defaults_to_dry_run(self):
        rc, out, fake = self.run_cli(["deploy", self.spec_path()])
        self.assertEqual(rc, 0)
        self.assertIn("[DRY RUN] Example Nightly Digest: create", out)
        self.assertIn("nothing was written", out)
        self.assertIn("disabled while dry_run is on: Notify", out)
        self.assertEqual(fake.writes(), [])

    def test_deploy_apply_writes(self):
        rc, out, fake = self.run_cli(["deploy", self.spec_path(), "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("[APPLIED]", out)
        self.assertEqual(len(fake.writes()), 1)

    def test_apply_with_unset_env_exits_2_and_writes_nothing(self):
        rc, out, fake = self.run_cli(["deploy", self.spec_path(), "--apply"], env={"N8N_API_KEY": "test-key"})
        self.assertEqual(rc, 2)
        self.assertEqual(fake.writes(), [])

    def test_dry_run_lists_unset_env(self):
        rc, out, _ = self.run_cli(["deploy", self.spec_path()], env={"N8N_API_KEY": "test-key"})
        self.assertEqual(rc, 0)
        self.assertIn("unset environment variables: N8N_CRED_NOTIFY_ID", out)

    def test_bad_spec_file_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.json")
            Path(p).write_text("{oops")
            rc, _, _ = self.run_cli(["deploy", p])
            self.assertEqual(rc, 2)
            rc, _, _ = self.run_cli(["deploy", os.path.join(d, "missing.json")])
            self.assertEqual(rc, 2)

    def test_build_writes_importable_json_offline(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, fake = self.run_cli(["build", self.spec_path(), self.spec_path("webhook-only"), "--out", d])
            self.assertEqual(rc, 0)
            self.assertEqual(sorted(os.listdir(d)), ["example-nightly-digest.workflow.json", "example-webhook-echo.workflow.json"])
            doc = json.loads((Path(d) / "example-nightly-digest.workflow.json").read_text())
            self.assertEqual(doc["name"], "Example Nightly Digest")
            self.assertEqual(fake.calls, [])
            self.assertIn("unset environment variables left as placeholders", out)

    def test_multiple_specs_in_one_deploy(self):
        env = {**ENV, "LOCAL_SERVICE_URL": "http://127.0.0.1:9000"}
        rc, out, fake = self.run_cli(["deploy", self.spec_path(), self.spec_path("webhook-only"), "--apply"], env=env)
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.workflows), 2)


class RepoHygiene(OfflineTestCase):
    def test_examples_and_docs_contain_no_literal_ids(self):
        import re
        for path in list((REPO / "examples").glob("*.json")):
            text = path.read_text()
            self.assertNotRegex(text, r'"chat_?id"\s*:\s*"?-?\d{5,}')
            self.assertNotRegex(text, r'"id"\s*:')


if __name__ == "__main__":
    unittest.main()
