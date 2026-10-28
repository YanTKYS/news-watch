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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import plamo_weekly_poc as poc
import weekly

KEY = "dummy-plamo-secret"


def valid_review():
    return {"overview": "今週の概況です。", "closing": "今週のまとめです。",
            "topics": [{"title": "見出し", "summary": "資料にある事実の要約。", "implication": "実務への示唆。",
                        "source_ids": ["article-001"]}]}


def completion(content=None):
    return {"model": poc.MODEL,
            "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(valid_review() if content is None else content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}


class Response:
    status = 200

    def __init__(self, data):
        self.raw = data if isinstance(data, bytes) else json.dumps(data).encode()

    def read(self, size):
        return self.raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class PlamoPoC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "runs"

    def run_poc(self, *, response=None, error=None, execute=True, spec_confirmed=True, env=None, name="test"):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(poc, "open_once", return_value=Response(completion() if response is None else response),
                               side_effect=error) as call, redirect_stdout(stdout), redirect_stderr(stderr):
            code = poc.run(execute=execute, spec_confirmed=spec_confirmed, output_root=self.out, run_name=name,
                           env={"PLAMO_API_KEY": KEY} if env is None else env)
        files = {p.name: p.read_text() for p in (self.out / name).glob("*") if p.is_file()}
        output = stdout.getvalue() + stderr.getvalue() + "".join(files.values())
        self.assertNotIn(KEY, output)
        return code, call, files, output

    def test_exact_w42_input_and_request_reuse(self):
        week, articles, manifest = poc.prepare(ROOT / "logs")
        self.assertEqual((week.label, len(articles)), ("2026-W42", 15))
        self.assertEqual(manifest["input_sha256"], poc.EXPECTED_INPUT_SHA256)
        req = poc.build_request(week, articles, KEY)
        body = json.loads(req.data)
        self.assertEqual(req.full_url, poc.ENDPOINT)
        self.assertEqual(req.get_header("Authorization"), f"Bearer {KEY}")
        self.assertEqual(body["messages"], [
            {"role": "system", "content": weekly.SYSTEM_PROMPT},
            {"role": "user", "content": weekly.build_user_prompt(week, articles)}])
        fmt = body["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["json_schema"]["strict"])
        self.assertEqual(fmt["json_schema"]["schema"], weekly.response_schema([a.id for a in articles]))
        self.assertNotIn("tools", body)
        self.assertNotIn(KEY, req.full_url + req.data.decode())
        self.assertTrue(all(a.url not in body["messages"][1]["content"] for a in articles))

    def test_offline_default_even_with_key(self):
        code, call, files, _ = self.run_poc(execute=False)
        self.assertEqual(code, 0)
        call.assert_not_called()
        self.assertNotIn("candidate.md", files)
        self.assertEqual(json.loads(files["metrics.json"])["api_requests"], 0)

    def test_missing_key_never_calls(self):
        code, call, files, _ = self.run_poc(env={})
        self.assertEqual(code, 2)
        call.assert_not_called()
        self.assertFalse(files)

    def test_unconfirmed_spec_never_calls_even_with_key(self):
        code, call, files, output = self.run_poc(spec_confirmed=False)
        self.assertEqual(code, 2)
        call.assert_not_called()
        self.assertFalse(files)
        self.assertIn("--spec-confirmed", output)

    def test_success_links_only_to_validated_inputs(self):
        code, call, files, _ = self.run_poc()
        self.assertEqual(code, 0)
        call.assert_called_once()
        metrics = json.loads(files["metrics.json"])
        self.assertEqual((metrics["api_requests"], metrics["http_status"], metrics["validation"]), (1, 200, "passed"))
        self.assertTrue(metrics["spec_confirmed"])
        self.assertEqual(metrics["usage"]["total_tokens"], 150)
        manifest = json.loads(files["input-manifest.json"])
        self.assertIn(weekly.md_url(manifest["articles"][0]["url"]), files["candidate.md"])
        self.assertIn(poc.MODEL, files["candidate.md"])
        self.assertIn("検証未完了", files["report.md"])

    def test_http_errors_never_retry_or_dump_error_body(self):
        for status in (302, 400, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                err = urllib.error.HTTPError(poc.ENDPOINT, status, KEY, {"secret": KEY}, io.BytesIO(KEY.encode()))
                code, call, files, _ = self.run_poc(error=err, name=f"http{status}")
                self.assertEqual(code, 1)
                call.assert_called_once()
                self.assertNotIn("candidate.md", files)
                metrics = json.loads(files["metrics.json"])
                self.assertEqual((metrics["api_requests"], metrics["http_status"]), (1, status))

    def test_transport_errors_never_retry_or_dump_messages(self):
        for i, err in enumerate((TimeoutError(KEY), urllib.error.URLError(KEY),
                                 poc.http.client.IncompleteRead(KEY.encode()))):
            code, call, files, _ = self.run_poc(error=err, name=f"transport{i}")
            self.assertEqual(code, 1)
            call.assert_called_once()
            self.assertNotIn("candidate.md", files)
            self.assertEqual(json.loads(files["metrics.json"])["api_requests"], 1)

    def test_invalid_envelopes_rejected(self):
        variants = [b"not JSON", [], {}, {"error": KEY}]
        for reason in ("length", "content_filter", None):
            data = completion()
            data["choices"][0]["finish_reason"] = reason
            variants.append(data)
        for content in (None, "", [], KEY):
            data = completion()
            data["choices"][0]["message"]["content"] = content
            variants.append(data)
        for field in ("tool_calls", "function_call", "refusal"):
            data = completion()
            data["choices"][0]["message"][field] = KEY
            variants.append(data)
        data = completion()
        data["model"] = "other-model"
        variants.append(data)
        data = completion()
        data["choices"] *= 2
        variants.append(data)
        for i, data in enumerate(variants):
            with self.subTest(i=i):
                code, call, files, _ = self.run_poc(response=data, name=f"invalid{i}")
                self.assertEqual(code, 1)
                call.assert_called_once()
                self.assertNotIn("candidate.md", files)
                self.assertEqual(json.loads(files["metrics.json"])["validation"], "failed")

    def test_invalid_reviews_rejected(self):
        variants = []
        for ids in (["unknown"], [KEY], ["article-001", "article-001"], [], [1]):
            data = valid_review()
            data["topics"][0]["source_ids"] = ids
            variants.append(data)
        data = valid_review()
        data["topics"] *= 2
        variants.append(data)
        for field, val in (("overview", "https://example.com"), ("overview", KEY),
                           ("overview", "x" * 1501), ("overview", ""), ("closing", 3), ("extra", True)):
            data = valid_review()
            data[field] = val
            variants.append(data)
        data = valid_review()
        data["topics"][0]["extra"] = True
        variants.append(data)
        for i, data in enumerate(variants):
            with self.subTest(i=i):
                code, call, files, _ = self.run_poc(response=completion(data), name=f"review{i}")
                self.assertEqual(code, 1)
                call.assert_called_once()
                self.assertNotIn("candidate.md", files)

    def test_oversize_response_rejected(self):
        code, call, files, _ = self.run_poc(response=b"x" * (weekly.MAX_RESPONSE_BYTES + 1))
        self.assertEqual(code, 1)
        call.assert_called_once()
        self.assertNotIn("candidate.md", files)

    def test_usage_copies_only_nonnegative_integer_counts(self):
        self.assertEqual(poc.usage_tokens({"usage": {"prompt_tokens": True, "completion_tokens": -1,
                                                  "total_tokens": 4, "secret": KEY}}), {"total_tokens": 4})
        self.assertEqual(poc.usage_tokens({}), {})

    def test_output_cannot_be_overwritten(self):
        self.run_poc(execute=False)
        code, call, files, _ = self.run_poc()
        self.assertEqual(code, 2)
        call.assert_not_called()
        self.assertNotIn("candidate.md", files)

    def test_path_traversal_rejected_before_api(self):
        code, call, _, _ = self.run_poc(name="../escape")
        self.assertEqual(code, 2)
        call.assert_not_called()

    def test_missing_or_drifted_input_rejected_before_api(self):
        with mock.patch.object(poc.weekly, "load_week_articles", return_value=([], 0)):
            code, call, _, _ = self.run_poc()
        self.assertEqual(code, 2)
        call.assert_not_called()
        week, articles, _ = poc.prepare(ROOT / "logs")
        changed = copy.deepcopy(articles)
        changed[0].description += "changed"
        with mock.patch.object(poc.weekly, "load_week_articles", return_value=(changed, 0)):
            code, call, _, _ = self.run_poc()
        self.assertEqual(code, 2)
        call.assert_not_called()

    def test_redirects_are_not_followed(self):
        self.assertIsNone(poc.NoRedirect().redirect_request(None, None, 302, "", {}, poc.ENDPOINT))

    def test_default_output_is_outside_production(self):
        self.assertEqual(poc.OUTPUT_ROOT.relative_to(ROOT).parts[:2], ("experiments", "plamo-w42"))


if __name__ == "__main__":
    unittest.main()
