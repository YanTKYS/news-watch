import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("sarashina_probe", ROOT / "experiments/sarashina-20261029/probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
KEY = "dummy-sarashina-secret"
PRIVATE = "DO_NOT_SAVE_RESPONSE_BODY"


def completion(content=PRIVATE, reason="stop"):
    return {"choices": [{"finish_reason": reason, "message": {"content": content}}]}


class Response:
    status = 200

    def __init__(self, data, headers=None):
        self.raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.headers = headers or {"Content-Type": "application/json", "Server": "nginx"}

    def read(self, size):
        return self.raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def http_error(status, content_type="application/json", server="nginx"):
    return urllib.error.HTTPError(probe.BASE_URL, status, KEY,
                                  {"Content-Type": content_type, "Server": server},
                                  io.BytesIO((KEY + PRIVATE).encode()))


class SarashinaProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "runs"

    def run_probe(self, responses=None, *, execute=True, env=None, models=True, name="test"):
        responses = responses if responses is not None else [Response(completion()), Response(completion()),
                        Response({"data": [{"id": probe.NEW_MODEL}, {"id": probe.OLD_MODEL}, {"id": PRIVATE}]})]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(probe, "open_once", side_effect=responses) as call, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            result = probe.run(execute=execute, include_models=models, output_root=self.out,
                               run_name=name, env={"SARASHINA_API_KEY": KEY} if env is None else env)
        files = {p.name: p.read_text() for p in (self.out / name).glob("*") if p.is_file()}
        text = stdout.getvalue() + stderr.getvalue() + "".join(files.values())
        self.assertNotIn(KEY, text)
        self.assertNotIn(PRIVATE, text)
        metrics = json.loads(files["metrics.json"]) if "metrics.json" in files else None
        if metrics:
            self.assertLessEqual(metrics["api_requests"], 3)
            self.assertEqual(metrics["api_requests"], call.call_count)
        return result, call, metrics, files

    def test_default_is_offline_even_with_key(self):
        result, call, metrics, files = self.run_probe(execute=False)
        self.assertEqual(result, 0)
        call.assert_not_called()
        self.assertEqual(metrics["status"], "not_run")
        self.assertEqual(set(files), {"README.md", "metrics.json", "report.md"})

    def test_missing_key_stops_before_sending_with_diagnostics(self):
        result, call, metrics, files = self.run_probe(env={})
        self.assertEqual(result, 2)
        call.assert_not_called()
        self.assertEqual(metrics["status"], "missing_key")
        self.assertIn("report.md", files)

    def test_request_parameters_use_current_model_and_token_field(self):
        for kind, model in (("new_chat", probe.NEW_MODEL), ("old_chat", probe.OLD_MODEL)):
            req = probe.build_request(kind, KEY)
            body = json.loads(req.data)
            self.assertEqual(req.full_url, "https://api.sarashina.ai/v1/chat/completions")
            self.assertEqual(req.method, "POST")
            self.assertEqual(req.get_header("Authorization"), "Bearer " + KEY)
            self.assertEqual(body["model"], model)
            self.assertNotIn("max_tokens", body)
            self.assertTrue(0 < body["max_completion_tokens"] <= 8000)
            self.assertFalse(body["stream"])
            self.assertEqual(body["messages"], [{"role": "user", "content": probe.PROMPT}])
            self.assertNotIn("tools", body)
            if kind == "new_chat":
                self.assertEqual(body["reasoning_effort"], "low")
            else:
                self.assertNotIn("reasoning_effort", body)
        req = probe.build_request("models", KEY)
        self.assertEqual(req.method, "GET")
        self.assertIsNone(req.data)
        self.assertEqual(req.full_url, "https://api.sarashina.ai/v1/models")

    def test_success_sends_new_old_then_models_once(self):
        result, call, metrics, files = self.run_probe()
        self.assertEqual(result, 0)
        self.assertEqual(call.call_count, 3)
        self.assertEqual([a["kind"] for a in metrics["attempts"]], ["new_chat", "old_chat", "models"])
        self.assertTrue(metrics["new_chat_success"])
        self.assertTrue(metrics["attempts"][2]["new_model_present"])
        self.assertTrue(metrics["attempts"][2]["old_model_present"])
        self.assertEqual(set(files), {"README.md", "metrics.json", "report.md"})

    def test_optional_models_can_be_omitted(self):
        result, call, metrics, _ = self.run_probe(models=False)
        self.assertEqual(result, 0)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(len(metrics["attempts"]), 2)

    def test_legacy_html_403_stops_immediately_without_reading_body(self):
        error = http_error(403, "text/html; charset=utf-8", "awselb/2.0")
        with mock.patch.object(error, "read", side_effect=AssertionError("must not read error body")):
            result, call, metrics, _ = self.run_probe([error])
        self.assertEqual(result, 1)
        call.assert_called_once()
        row = metrics["attempts"][0]
        self.assertTrue(row["matches_legacy_header_signature"])
        self.assertEqual((row["http_status"], row["content_type"], row["server"]), (403, "text/html", "awselb/2.0"))
        self.assertFalse(metrics["new_chat_success"])

    def test_all_new_model_http_errors_stop_without_retry(self):
        for status in (301, 302, 400, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                result, call, metrics, _ = self.run_probe([http_error(status)], name=f"http{status}")
                self.assertEqual(result, 1)
                call.assert_called_once()
                self.assertEqual(metrics["attempts"][0]["http_status"], status)
                self.assertFalse(metrics["attempts"][0]["matches_legacy_header_signature"])

    def test_transport_failures_do_not_retry_or_leak_messages(self):
        for i, error in enumerate((TimeoutError(KEY), urllib.error.URLError(KEY),
                                   probe.http.client.IncompleteRead(PRIVATE.encode()))):
            result, call, metrics, _ = self.run_probe([error], name=f"transport{i}")
            self.assertEqual(result, 1)
            call.assert_called_once()
            self.assertEqual(metrics["attempts"][0]["http_class"], "no_response")

    def test_200_invalid_completions_do_not_allow_old_or_models(self):
        invalid = [b"<html>" + PRIVATE.encode(), b"invalid-json", {}, [],
                   completion(""), completion("   "), completion(None),
                   completion([], "stop"), completion(reason="length"), completion(reason=None)]
        data = completion();data["choices"][0]["message"]["tool_calls"] = [PRIVATE];invalid.append(data)
        data = completion();data["choices"][0]["message"]["refusal"] = PRIVATE;invalid.append(data)
        for i, data in enumerate(invalid):
            result, call, metrics, _ = self.run_probe([Response(data)], name=f"invalid{i}")
            self.assertEqual(result, 1)
            call.assert_called_once()
            self.assertFalse(metrics["new_chat_success"])
            self.assertEqual(metrics["attempts"][0]["http_status"], 200)

    def test_oversize_response_stops(self):
        result, call, metrics, _ = self.run_probe([Response(b"x" * (probe.MAX_RESPONSE_BYTES + 1))])
        self.assertEqual(result, 1)
        call.assert_called_once()
        self.assertEqual(metrics["attempts"][0]["result"], "response_too_large")

    def test_unknown_headers_are_never_saved_verbatim(self):
        result, _, metrics, _ = self.run_probe([http_error(403, KEY, PRIVATE)])
        row = metrics["attempts"][0]
        self.assertEqual(result, 1)
        self.assertEqual((row["content_type"], row["server"]), ("other", "other"))

    def test_old_failure_is_not_confused_with_new_success(self):
        result, call, metrics, _ = self.run_probe([Response(completion()), http_error(403),
                                                  Response({"data": []})])
        self.assertEqual(result, 1)
        self.assertTrue(metrics["new_chat_success"])
        self.assertEqual(metrics["status"], "comparison_failed")
        self.assertEqual(call.call_count, 3)

    def test_models_failure_does_not_revoke_chat_success_or_trigger_retry(self):
        for i, data in enumerate(({}, {"data": [PRIVATE]}, {"data": [{"id": 1}]}, {"error": KEY})):
            result, call, metrics, _ = self.run_probe([Response(completion()), Response(completion()),
                                                     Response(data)], name=f"models{i}")
            self.assertEqual(result, 1)
            self.assertTrue(metrics["new_chat_success"])
            self.assertEqual(call.call_count, 3)
            self.assertEqual(metrics["attempts"][2]["result"], "invalid_models")

    def test_request_budget_is_enforced_independently(self):
        client = probe.Probe()
        with mock.patch.object(probe, "open_once", side_effect=[Response(completion()) for _ in range(3)]) as call:
            for _ in range(3):
                client.attempt("new_chat", KEY)
            with self.assertRaises(RuntimeError):
                client.attempt("new_chat", KEY)
        self.assertEqual(call.call_count, 3)

    def test_redirects_cannot_add_another_request(self):
        self.assertIsNone(probe.NoRedirect().redirect_request(None, None, 302, "", {}, probe.BASE_URL))

    def test_existing_output_is_not_overwritten(self):
        self.run_probe(execute=False)
        result, call, _, _ = self.run_probe()
        self.assertEqual(result, 2)
        call.assert_not_called()

    def test_production_paths_are_not_outputs(self):
        self.assertEqual(probe.OUTPUT_ROOT.relative_to(ROOT).parts,
                         ("experiments", "sarashina-20261029", "runs"))
        result, call, _, _ = self.run_probe(name="../escape")
        self.assertEqual(result, 2)
        call.assert_not_called()


class SarashinaWorkflow(unittest.TestCase):
    def setUp(self):
        self.config = yaml.load((ROOT / ".github/workflows/sarashina-20261029-poc.yml").read_text(),
                                Loader=yaml.BaseLoader)
        self.job = self.config["jobs"]["connectivity"]

    def test_manual_only_offline_by_default_with_read_only_permissions(self):
        self.assertEqual(set(self.config["on"]), {"workflow_dispatch"})
        self.assertEqual(self.config["on"]["workflow_dispatch"]["inputs"]["execute"]["default"], "false")
        self.assertEqual(self.config["permissions"], {"contents": "read"})
        self.assertEqual(self.job["runs-on"], "ubuntu-latest")

    def test_secret_is_only_in_execute_step(self):
        steps = [s for s in self.job["steps"] if "SARASHINA_API_KEY" in s.get("env", {})]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["if"], "${{ inputs.execute }}")
        self.assertEqual(steps[0]["env"]["SARASHINA_API_KEY"], "${{ secrets.SARASHINA_API_KEY }}")
        self.assertEqual(steps[0]["run"].count("probe.py"), 1)

    def test_artifact_is_a_safe_file_allowlist_even_on_failure(self):
        upload = next(s for s in self.job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["if"], "${{ always() }}")
        paths = upload["with"]["path"].splitlines()
        self.assertEqual(len(paths), 3)
        self.assertEqual({p.rsplit("/", 1)[1] for p in paths}, {"README.md", "metrics.json", "report.md"})
        self.assertTrue(all(p.startswith("experiments/sarashina-20261029/runs/actions-") and "*" not in p
                            for p in paths))


if __name__ == "__main__":
    unittest.main()
