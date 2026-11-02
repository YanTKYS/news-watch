import copy
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import w43_api_compare as compare
import w43_batch_compare as batch
from test_weekly import FakeResponse, completion, review

KEYS = {compare.GEMINI_MODEL: "dummy-w43-gemini-secret", compare.PLAMO_MODEL: "dummy-w43-plamo-secret"}


class Response(FakeResponse):
    status = 200


def reply(model, content=None, reason=None):
    content = review() if content is None else content
    if model == compare.GEMINI_MODEL:
        return completion(content, finish_reason=reason or "STOP",
                          usage={"promptTokenCount": 100, "candidatesTokenCount": 50, "totalTokenCount": 150})
    return {"model": compare.PLAMO_MODEL,
            "choices": [{"finish_reason": reason or "stop", "message": {
                "content": content if isinstance(content, str) else json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}


class W43Comparison(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"GEMINI_API_KEY": KEYS[compare.GEMINI_MODEL], "PLAMO_API_KEY": KEYS[compare.PLAMO_MODEL],
                    "GEMINI_MODEL": "gemini-3.8-flash"}

    def run_compare(self, model, *, response=None, error=None, execute=True, confirmed=True, env=None, name="test"):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(compare.plamo, "open_once", return_value=Response(reply(model) if response is None else response),
                               side_effect=error) as call, \
                mock.patch.object(compare.weekly, "request_gemini", side_effect=AssertionError("must not use retry driver")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = compare.run(model=model, execute=execute, spec_confirmed=confirmed, run_name=name,
                               env=self.env if env is None else env, output_root=self.root)
        files = {p.name: p.read_text() for p in (self.root / model / name).glob("*") if p.is_file()}
        text = stdout.getvalue() + stderr.getvalue() + "".join(files.values())
        for key in KEYS.values():
            self.assertNotIn(key, text)
        metrics = json.loads(files["metrics.json"]) if "metrics.json" in files else None
        if metrics:
            self.assertLessEqual(metrics["api_requests"], 1)
            self.assertEqual(metrics["api_requests"], call.call_count)
        return code, call, metrics, files

    def test_fixed_w43_manifest_and_w42_baseline_untouched(self):
        week, articles, manifest = compare.prepare(ROOT / "logs")
        self.assertEqual((week.label, len(articles)), ("2026-W43", 26))
        self.assertEqual([a.id for a in articles], [f"article-{i:03d}" for i in range(1, 27)])
        self.assertEqual([a.url for a in articles], [a["url"] for a in manifest["articles"]])
        for key, expected in compare.EXPECTED_HASHES.items():
            self.assertEqual(manifest[key], expected)
        self.assertEqual(manifest["schema_adjustments"], [])
        old_week, old_articles, _ = compare.plamo.prepare(ROOT / "logs")
        self.assertEqual((old_week.label, len(old_articles)), ("2026-W42", 15))

    def test_preparation_never_sends_with_keys_present(self):
        for model in compare.MODELS:
            code, call, metrics, files = self.run_compare(model, execute=False)
            self.assertEqual(code, 0)
            call.assert_not_called()
            self.assertEqual(metrics["status"], "not_run")
            self.assertEqual(set(files), {"input-manifest.json", "metrics.json", "report.md"})

    def test_only_selected_key_is_accepted_and_spec_confirmation_required(self):
        for model in compare.MODELS:
            other_key = {"PLAMO_API_KEY": self.env["PLAMO_API_KEY"]} if model == compare.GEMINI_MODEL else {
                "GEMINI_API_KEY": self.env["GEMINI_API_KEY"]}
            for i, (env, confirmed, expected) in enumerate(((other_key, True, "missing_key"),
                                                          (self.env, False, "spec_unconfirmed"))):
                code, call, metrics, _ = self.run_compare(model, env=env, confirmed=confirmed, name=f"blocked{i}")
                self.assertEqual(code, 2)
                call.assert_not_called()
                self.assertEqual(metrics["error"], expected)

    def test_success_uses_correct_request_and_validated_source_links(self):
        week, articles, manifest = compare.prepare(ROOT / "logs")
        for model in compare.MODELS:
            code, call, metrics, files = self.run_compare(model)
            self.assertEqual(code, 0)
            call.assert_called_once()
            self.assertEqual((metrics["http_status"], metrics["validation"]), (200, "passed"))
            self.assertEqual(metrics["model"], model)
            self.assertEqual(metrics["finish_reason"], "STOP" if model == compare.GEMINI_MODEL else "stop")
            self.assertTrue(metrics["usage"])
            self.assertIn(compare.weekly.md_url(articles[0].url), files["candidate.md"])
            self.assertIn(model, files["candidate.md"])
            request = call.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(request.method, "POST")
            if model == compare.GEMINI_MODEL:
                self.assertIn("/models/gemini-3.6-flash:generateContent", request.full_url)
                self.assertEqual(request.get_header("X-goog-api-key"), KEYS[model])
                self.assertEqual(body["systemInstruction"]["parts"][0]["text"], compare.weekly.SYSTEM_PROMPT)
                self.assertEqual(body["contents"][0]["parts"][0]["text"], compare.weekly.build_user_prompt(week, articles))
                schema = body["generationConfig"]["responseJsonSchema"]
            else:
                self.assertEqual(request.full_url, compare.plamo.ENDPOINT)
                self.assertEqual(request.get_header("Authorization"), "Bearer " + KEYS[model])
                self.assertEqual(body["model"], model)
                self.assertEqual(body["messages"][0]["content"], compare.weekly.SYSTEM_PROMPT)
                self.assertEqual(body["messages"][1]["content"], compare.weekly.build_user_prompt(week, articles))
                schema = body["response_format"]["json_schema"]["schema"]
            self.assertEqual(compare.plamo.digest(schema), manifest["schema_sha256"])
            self.assertNotIn("tools", body)

    def test_http_errors_do_not_retry_fallback_or_expose_body(self):
        for model in compare.MODELS:
            for status in (302, 403, 429, 503):
                with self.subTest(model=model, status=status):
                    err = urllib.error.HTTPError("https://example.invalid", status, KEYS[model], {},
                                                 io.BytesIO(KEYS[model].encode()))
                    with mock.patch.object(err, "read", side_effect=AssertionError("do not read error body")):
                        code, call, metrics, files = self.run_compare(model, error=err, name=f"http{status}")
                    self.assertEqual(code, 1)
                    call.assert_called_once()
                    self.assertEqual(metrics["http_status"], status)
                    self.assertNotIn("candidate.md", files)

    def test_timeouts_and_disconnects_stop_after_one_attempt(self):
        for model in compare.MODELS:
            for i, error in enumerate((TimeoutError(KEYS[model]), urllib.error.URLError(KEYS[model]),
                                       compare.http.client.IncompleteRead(KEYS[model].encode()))):
                code, call, metrics, files = self.run_compare(model, error=error, name=f"transport{i}")
                self.assertEqual(code, 1)
                call.assert_called_once()
                self.assertEqual(metrics["error"], "transport_or_io_error")
                self.assertNotIn("candidate.md", files)

    def test_bad_json_and_nonobjects_never_save_candidate(self):
        for model in compare.MODELS:
            for i, data in enumerate((b"invalid-json", [], {"error": KEYS[model]}, b"x" * (compare.weekly.MAX_RESPONSE_BYTES + 1))):
                code, call, metrics, files = self.run_compare(model, response=data, name=f"json{i}")
                self.assertEqual(code, 1)
                call.assert_called_once()
                self.assertEqual(metrics["validation"], "failed")
                self.assertNotIn("candidate.md", files)

    def test_truncated_or_unknown_finish_reason_is_not_a_valid_review(self):
        for model in compare.MODELS:
            reason = "MAX_TOKENS" if model == compare.GEMINI_MODEL else "length"
            for i, value in enumerate((reason, KEYS[model])):
                code, call, metrics, files = self.run_compare(model, response=reply(model, reason=value), name=f"finish{i}")
                self.assertEqual(code, 1)
                call.assert_called_once()
                self.assertEqual(metrics["finish_reason"], reason if i == 0 else "other")
                self.assertNotIn("candidate.md", files)

    def test_invalid_review_fields_and_ids_are_rejected_for_both_models(self):
        for model in compare.MODELS:
            variants = []
            for ids in (["unknown"], [KEYS[model]], ["article-001", "article-001"], []):
                data = review()
                data["topics"][0]["source_ids"] = ids
                variants.append(data)
            data = review()
            data["topics"] *= 2
            variants.append(data)
            for field, value in (("extra", True), ("overview", "https://example.com"), ("overview", KEYS[model])):
                data = review()
                data[field] = value
                variants.append(data)
            for i, data in enumerate(variants):
                code, call, metrics, files = self.run_compare(model, response=reply(model, data), name=f"review{i}")
                self.assertEqual(code, 1)
                call.assert_called_once()
                self.assertNotIn("candidate.md", files)

    def test_input_count_content_order_prompts_and_schema_are_fixed(self):
        _, original, _ = compare.prepare(ROOT / "logs")
        modifications = [original[:-1], list(reversed(copy.deepcopy(original)))]
        changed = copy.deepcopy(original)
        changed[0].description += "changed"
        modifications.append(changed)
        for i, articles in enumerate(modifications):
            with mock.patch.object(compare.weekly, "load_week_articles", return_value=(articles, 0)):
                code, call, _, _ = self.run_compare(compare.GEMINI_MODEL, name=f"input{i}")
            self.assertEqual(code, 2)
            call.assert_not_called()
        with mock.patch.object(compare.weekly, "SYSTEM_PROMPT", "changed"):
            code, call, _, _ = self.run_compare(compare.GEMINI_MODEL, name="prompt")
        self.assertEqual(code, 2)
        call.assert_not_called()
        with mock.patch.object(compare.weekly, "response_schema", return_value={}):
            code, call, _, _ = self.run_compare(compare.GEMINI_MODEL, name="schema")
        self.assertEqual(code, 2)
        call.assert_not_called()

    def test_model_results_are_separate_and_existing_runs_cannot_be_overwritten(self):
        for model in compare.MODELS:
            code, _, _, _ = self.run_compare(model, execute=False)
            self.assertEqual(code, 0)
            code, call, _, _ = self.run_compare(model)
            self.assertEqual(code, 2)
            call.assert_not_called()
        self.assertEqual(set(p.name for p in self.root.iterdir()), set(compare.MODELS))
        self.assertEqual(compare.OUTPUT_ROOT.relative_to(ROOT).parts, ("experiments", "w43-api-compare", "runs"))

    def test_invalid_model_and_path_never_send(self):
        for model, name in (("gemini-3.8-flash", "no-model"), (compare.GEMINI_MODEL, "../escape")):
            code, call, _, _ = self.run_compare(model, name=name)
            self.assertEqual(code, 2)
            call.assert_not_called()


class W43Workflow(unittest.TestCase):
    def setUp(self):
        self.wf = yaml.load((ROOT / ".github/workflows/w43-api-compare.yml").read_text(), Loader=yaml.BaseLoader)
        self.steps = self.wf["jobs"]["compare"]["steps"]

    def test_only_manual_batch_without_automatic_execution(self):
        self.assertEqual(set(self.wf["on"]), {"workflow_dispatch"})
        inputs = self.wf["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"execute", "spec_confirmed"})
        self.assertEqual(inputs["execute"]["default"], "false")
        self.assertEqual(inputs["spec_confirmed"]["default"], "false")
        self.assertEqual(self.wf["permissions"], {"contents": "read"})

    def test_preflight_gates_both_independent_api_steps_and_final_status(self):
        prepare = next(s for s in self.steps if s.get("id") == "prepare")
        self.assertNotIn("GEMINI_API_KEY", prepare.get("env", {}))
        self.assertNotIn("PLAMO_API_KEY", prepare.get("env", {}))
        gate = next(s for s in self.steps if s.get("id") == "credentials")
        self.assertEqual(set(gate["env"]), {"GEMINI_API_KEY", "PLAMO_API_KEY"})
        self.assertIn("check-credentials", gate["run"])
        self.assertIn("steps.prepare.outcome == 'success'", gate["if"])
        for model, key in ((compare.GEMINI_MODEL, "GEMINI_API_KEY"), (compare.PLAMO_MODEL, "PLAMO_API_KEY")):
            steps = [s for s in self.steps if "w43_api_compare.py" in s.get("run", "") and key in s.get("env", {})]
            self.assertEqual(len(steps), 1)
            step = steps[0]
            self.assertEqual(set(step["env"]), {key})
            self.assertEqual(step["if"], "${{ !cancelled() && inputs.execute && steps.credentials.outcome == 'success' }}")
            self.assertEqual(step["continue-on-error"], "true")
            self.assertIn("--model " + model, step["run"])
            self.assertEqual(step["run"].count("w43_api_compare.py"), 1)
        summary = next(s for s in self.steps if " summarize " in s.get("run", ""))
        self.assertEqual(summary["if"], "${{ always() }}")
        self.assertNotIn("continue-on-error", summary)
        self.assertEqual(summary["env"], {"GEMINI_OUTCOME": "${{ steps.gemini.outcome }}",
                                         "PLAMO_OUTCOME": "${{ steps.plamo.outcome }}"})

    def test_artifact_is_an_explicit_allowlist_on_success_or_failure(self):
        upload = next(s for s in self.steps if s.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["if"], "${{ always() }}")
        paths = upload["with"]["path"].splitlines()
        self.assertEqual(len(paths), 11)
        self.assertTrue(all(p.startswith("experiments/w43-api-compare/runs/") and "*" not in p for p in paths))
        self.assertEqual({p.rsplit("/", 1)[1] for p in paths},
                         {"input-manifest.json", "metrics.json", "report.md", "candidate.md",
                          "preflight.json", "comparison.json", "comparison.md"})
        for model in compare.MODELS:
            self.assertEqual(sum("/" + model + "/" in p for p in paths), 4)


class W43Batch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"GEMINI_API_KEY": KEYS[compare.GEMINI_MODEL], "PLAMO_API_KEY": KEYS[compare.PLAMO_MODEL]}

    def simulate_steps(self, name, *, execute=True, confirmed=True, env=None, responses=None):
        env = self.env if env is None else env
        responses = responses or [Response(reply(model)) for model in compare.MODELS]
        summary = self.root / (name + "-job-summary.md")
        with mock.patch.object(compare.plamo, "open_once", side_effect=responses) as call, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            prepared = batch.prepare(name, execute=execute, spec_confirmed=confirmed, root=self.root)
            ready = batch.check_credentials(name, env=env, root=self.root) if prepared == 0 and execute else 2
            if ready == 0:
                # Same independent steps/secret scopes as Actions: failure must not stop the next provider.
                for model, key in zip(compare.MODELS, ("GEMINI_API_KEY", "PLAMO_API_KEY")):
                    compare.run(model=model, execute=True, spec_confirmed=True, run_name=name,
                                output_root=self.root, env={key: env[key]})
            code = batch.summarize(name, root=self.root, summary_path=summary)
        folder = self.root / "comparison" / name
        info = json.loads((folder / "comparison.json").read_text())
        text = (folder / "comparison.md").read_text()
        self.assertEqual(summary.read_text(), text)
        for path in self.root.rglob("*"):
            if path.is_file():
                for key in KEYS.values():
                    self.assertNotIn(key, path.read_text())
        return code, call, info, text

    def test_one_offline_run_prepares_both_without_sending(self):
        code, call, info, text = self.simulate_steps("offline", execute=False)
        self.assertEqual(code, 0)
        call.assert_not_called()
        self.assertEqual(info["status"], "prepared")
        self.assertTrue(info["prepared_inputs_match"])
        self.assertTrue(info["result_inputs_match"])
        self.assertEqual([row["api_requests"] for row in info["models"]], [0, 0])
        self.assertTrue(all(model in text for model in compare.MODELS))

    def test_one_execution_attempts_each_model_once_and_combines_both(self):
        code, call, info, text = self.simulate_steps("success")
        self.assertEqual(code, 0)
        self.assertEqual(call.call_count, 2)
        self.assertIn("gemini-3.6-flash", call.call_args_list[0].args[0].full_url)
        self.assertEqual(call.call_args_list[1].args[0].full_url, compare.plamo.ENDPOINT)
        self.assertEqual([row["api_requests"] for row in info["models"]], [1, 1])
        self.assertTrue(all(row["candidate"] for row in info["models"]))
        self.assertEqual(info["status"], "succeeded")
        self.assertTrue(info["result_inputs_match"])
        for expected in ("実行日時", "HTTP", "完了理由", "usage", "JSON/出典ID検証"):
            self.assertIn(expected, text)
        # Even a late step failure after writing valid metrics must fail the batch.
        result = batch.summarize("success", root=self.root,
                                outcomes={compare.GEMINI_MODEL: "failure", compare.PLAMO_MODEL: "success"})
        self.assertEqual(result, 1)

    def test_either_missing_secret_or_unconfirmed_spec_blocks_both(self):
        for i, (env, confirmed, error) in enumerate((({}, True, "missing_credentials"),
                ({"GEMINI_API_KEY": self.env["GEMINI_API_KEY"]}, True, "missing_credentials"),
                ({"PLAMO_API_KEY": self.env["PLAMO_API_KEY"]}, True, "missing_credentials"),
                (self.env, False, "spec_unconfirmed"))):
            with self.subTest(case=i):
                code, call, info, text = self.simulate_steps(f"blocked{i}", env=env, confirmed=confirmed)
                self.assertEqual(code, 2)
                call.assert_not_called()
                self.assertEqual(info["status"], "blocked")
                self.assertEqual([row["api_requests"] for row in info["models"]], [0, 0])
                self.assertIn(error, text)

    def test_one_api_failure_still_runs_other_and_final_result_fails(self):
        cases = [
            [urllib.error.HTTPError("https://example.invalid", 503, KEYS[compare.GEMINI_MODEL], {},
                                    io.BytesIO(KEYS[compare.GEMINI_MODEL].encode())), Response(reply(compare.PLAMO_MODEL))],
            [Response(reply(compare.GEMINI_MODEL)), TimeoutError(KEYS[compare.PLAMO_MODEL])],
            [Response(reply(compare.GEMINI_MODEL, {"bad": "shape"})), Response(reply(compare.PLAMO_MODEL))],
        ]
        for i, responses in enumerate(cases):
            code, call, info, text = self.simulate_steps(f"partial{i}", responses=responses)
            self.assertEqual(code, 1)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(info["status"], "partial_failure")
            self.assertEqual(sum(row["candidate"] for row in info["models"]), 1)
            self.assertIn("partial_failure", text)
            self.assertTrue(info["result_inputs_match"])

    def test_both_failures_are_reported_without_any_extra_attempt(self):
        code, call, info, _ = self.simulate_steps("failed", responses=[TimeoutError(KEYS[compare.GEMINI_MODEL]),
                                                                      Response(b"bad JSON")])
        self.assertEqual(code, 1)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(info["status"], "failed")
        self.assertFalse(any(row["candidate"] for row in info["models"]))

    def test_input_disagreement_blocks_both_before_credentials_or_sending(self):
        prepared = compare.prepare(ROOT / "logs")
        changed = copy.deepcopy(prepared)
        changed[2]["articles"].reverse()
        with mock.patch.object(compare, "prepare", side_effect=[prepared, changed]):
            code, call, info, text = self.simulate_steps("mismatched")
        self.assertEqual(code, 2)
        call.assert_not_called()
        self.assertFalse(info["prepared_inputs_match"])
        self.assertIn("input_mismatch", text)

    def test_summary_normalizes_strings_and_reports_missing_results_as_unknown(self):
        self.assertEqual(batch.prepare("missing", execute=True, spec_confirmed=True, root=self.root), 0)
        self.assertEqual(batch.check_credentials("missing", env=self.env, root=self.root), 0)
        self.assertEqual(batch.summarize("missing", root=self.root), 1)
        info = json.loads((self.root / "comparison/missing/comparison.json").read_text())
        self.assertIsNone(info["result_inputs_match"])
        self.assertTrue(all(row["api_requests"] is None for row in info["models"]))
        row = batch.safe_row(compare.GEMINI_MODEL, {"status": KEYS[compare.GEMINI_MODEL], "error": KEYS[compare.PLAMO_MODEL],
                             "usage": {"secret": KEYS[compare.PLAMO_MODEL]}, "finish_reason": KEYS[compare.GEMINI_MODEL]}, False)
        for key in KEYS.values():
            self.assertNotIn(key, json.dumps(row))


if __name__ == "__main__":
    unittest.main()
