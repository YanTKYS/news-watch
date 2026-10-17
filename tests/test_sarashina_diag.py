import io
import json
import socket
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import sarashina_diag  # noqa: E402
import weekly  # noqa: E402

SECRET = "dummy-sarashina-key-for-tests-0123456789"  # 実キーではないテスト用ダミー値
ENV = {"SARASHINA_API_KEY": SECRET, "GITHUB_ACTIONS": "true"}


def headers(**values):
    msg = Message()
    for k, v in values.items():
        msg[k.replace("_", "-")] = v
    return msg


class FakeResponse:
    def __init__(self, payload, status=200, hdrs=None):
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.status = status
        self.headers = hdrs or headers(content_type="application/json", x_request_id="req-ok")

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok_minimal():
    return FakeResponse({"choices": [{"message": {"role": "assistant", "content": "こんにちは！"}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}})


def ok_structured():
    content = {"overview": "概況です。", "closing": "まとめです。",
               "topics": [{"title": "見出し", "summary": "要約。", "implication": "示唆。", "source_ids": ["article-001"]}]}
    return FakeResponse({"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}, "finish_reason": "stop"}]})


def http_error(status, body, hdrs=None):
    hdrs = hdrs or headers(content_type="application/json")
    return urllib.error.HTTPError(weekly.API_ENDPOINT, status, f"Forbidden {SECRET}", hdrs, io.BytesIO(body))


FORBIDDEN = json.dumps({"error": {"code": "forbidden", "type": "permission_error",
                                  "message": "Access denied for this request"}}).encode()


class Diagnostics(unittest.TestCase):
    def go(self, *responses, env=ENV):
        """responses: Aの応答, Bの応答（FakeResponse か 例外）。送信したリクエストを記録する。"""
        sent = []
        queue = list(responses)

        def responder(req, timeout=None):
            sent.append((req, timeout))
            r = queue.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sarashina_diag.urllib.request, "urlopen", side_effect=responder) as m, \
                mock.patch.object(weekly, "write_atomic", side_effect=AssertionError("must not write")), \
                redirect_stdout(out), redirect_stderr(err):
            code = sarashina_diag.run(env)
        self.assertLessEqual(m.call_count, 2)  # 診断全体で最大2リクエスト（リトライなし）
        text = out.getvalue() + err.getvalue()
        self.assertNotIn(SECRET, text)
        self.assertNotRegex(text, r"(?i)authorization\s*[:=]\s*(?!\*\*\*)\S")  # 認証情報の値を出さない
        self.assertNotRegex(text, r"(?i)bearer\s+(?!\*\*\*)\S")
        return code, sent, out.getvalue(), err.getvalue()

    def section(self, out, name):
        return out.split(name, 1)[1].split("\n\n", 1)[0]

    def test_both_success(self):
        code, sent, out, _ = self.go(ok_minimal(), ok_structured())
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 2)
        for line in ("Sarashina API diagnostics", "Environment: GitHub Actions", "Model: sarashina3-mini",
                     "Total API requests: 2", "A・Bとも成功"):
            self.assertIn(line, out)
        a, b = self.section(out, "Test A: Minimal request"), self.section(out, "Test B: Structured request")
        self.assertIn("HTTP status: 200\nResult: SUCCESS", a)
        self.assertIn("Tokens: prompt=5, completion=7, total=12", a)
        self.assertIn("Response header x-request-id: req-ok", a)
        self.assertIn("Structured output: valid (1 topics)", b)
        self.assertNotIn("こんにちは！", out)  # 応答本文は出さない
        self.assertNotIn("概況です", out)

    def test_requests_shape(self):
        _, sent, _, _ = self.go(ok_minimal(), ok_structured())
        (a, ta), (b, tb) = sent
        self.assertEqual((ta, tb), (weekly.TIMEOUT_SEC, weekly.TIMEOUT_SEC))
        for req in (a, b):
            self.assertEqual(req.full_url, "https://api.sarashina.ai/v1/chat/completions")
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(req.get_header("Authorization"), f"Bearer {SECRET}")
        self.assertEqual(dict(a.header_items()), dict(b.header_items()))  # ヘッダーは同一。差分は本文のみ
        body_a, body_b = json.loads(a.data), json.loads(b.data)
        self.assertEqual(body_a, {"model": "sarashina3-mini", "messages": [{"role": "user", "content": "こんにちは"}]})
        # Bは週刊生成と同じ build_request の形式
        self.assertEqual(set(body_b), {"model", "messages", "response_format", "max_completion_tokens"})
        self.assertEqual(body_b["response_format"]["type"], "json_schema")
        self.assertTrue(body_b["response_format"]["json_schema"]["strict"])
        self.assertEqual(body_b["max_completion_tokens"], weekly.MAX_COMPLETION_TOKENS)
        self.assertIn("（診断用）", body_b["messages"][1]["content"])

    def test_both_403(self):
        code, sent, out, _ = self.go(http_error(403, FORBIDDEN), http_error(403, FORBIDDEN))
        self.assertEqual(code, 1)
        self.assertEqual(len(sent), 2)  # Aが失敗してもBを実行する
        for name in ("Test A: Minimal request", "Test B: Structured request"):
            sec = self.section(out, name)
            self.assertIn("HTTP status: 403\nResult: FAILED", sec)
            self.assertIn("Error code: forbidden", sec)
            self.assertIn("Error type: permission_error", sec)
            self.assertIn("Error message: Access denied for this request", sec)
        self.assertIn("A・Bとも失敗", out)

    def test_a_success_b_403(self):
        code, _, out, _ = self.go(ok_minimal(), http_error(403, FORBIDDEN))
        self.assertEqual(code, 1)
        self.assertIn("Result: SUCCESS", self.section(out, "Test A"))
        self.assertIn("HTTP status: 403", self.section(out, "Test B"))
        self.assertIn("Aのみ成功", out)

    def test_a_403_b_success(self):
        code, _, out, _ = self.go(http_error(403, FORBIDDEN), ok_structured())
        self.assertEqual(code, 1)
        self.assertIn("Bのみ成功", out)

    def test_timeout_and_network_errors(self):
        code, sent, out, _ = self.go(TimeoutError("timed out"), urllib.error.URLError(socket.gaierror(-2, "Name or service not known")))
        self.assertEqual(code, 1)
        self.assertEqual(len(sent), 2)
        a, b = self.section(out, "Test A"), self.section(out, "Test B")
        self.assertIn("HTTP status: - (no response)", a)
        self.assertIn("Network error: TimeoutError", a)
        self.assertIn("Network error: URLError (gaierror:", b)

    def test_non_json_error_body(self):
        html = b"<html><title>Attention Required</title><body>secret page " + SECRET.encode() + b"</body></html>"
        hdrs = headers(content_type="text/html; charset=UTF-8", server="cloudflare", cf_ray="abc123-NRT",
                       set_cookie=f"session={SECRET}", www_authenticate="Bearer realm=x")
        code, _, out, _ = self.go(http_error(403, html, hdrs), http_error(403, b"{broken json"))
        self.assertEqual(code, 1)
        a = self.section(out, "Test A")
        self.assertIn(f"Error body: not JSON (text/html; charset=UTF-8, {len(html)} bytes)", a)
        self.assertIn("Response header server: cloudflare", a)
        self.assertIn("Response header cf-ray: abc123-NRT", a)
        self.assertNotIn("Attention Required", out)  # 本文は出さない
        self.assertNotIn("set-cookie", out.lower())
        self.assertNotIn("www-authenticate", out.lower())
        self.assertIn("Error body: not JSON (application/json, 12 bytes)", self.section(out, "Test B"))

    def test_error_message_masks_secret_and_is_truncated(self):
        body = json.dumps({"error": {"code": 403, "message": f"invalid key {SECRET} / Authorization: Bearer abc.def " + "x" * 1000}})
        detail = json.dumps({"detail": [{"loc": ["body", "response_format"], "msg": "not allowed", "type": "x"}]})
        code, _, out, _ = self.go(http_error(403, body.encode()), http_error(400, detail.encode()))
        a = self.section(out, "Test A")
        msg = next(line for line in a.splitlines() if line.startswith("Error message: "))
        self.assertIn("invalid key *** / Authorization: *** x", msg)
        self.assertLessEqual(len(msg), len("Error message: ") + sarashina_diag.MAX_TEXT_CHARS)
        self.assertIn("Error code: 403", a)
        self.assertIn("Error detail: body.response_format: not allowed", self.section(out, "Test B"))

    def test_json_error_without_known_fields(self):
        _, _, out, _ = self.go(http_error(403, b'{"foo": 1}'), http_error(403, b"[]"))
        self.assertIn("Error body: JSON without known error fields (keys: foo)", out)
        self.assertIn("Error body: JSON list without known error fields", out)

    def test_invalid_structured_output_is_reported(self):
        bad = FakeResponse({"choices": [{"message": {"content": "{not json"}, "finish_reason": "length"}]})
        code, _, out, _ = self.go(ok_minimal(), bad)
        self.assertEqual(code, 0)  # HTTPとしては成功
        b = self.section(out, "Test B")
        self.assertIn("Finish reason: length", b)
        self.assertIn("Structured output: invalid (output is not valid JSON)", b)

    def test_missing_key_makes_no_request(self):
        for env in ({}, {"SARASHINA_API_KEY": "  "}):
            code, sent, out, err = self.go(env=env)
            self.assertEqual(code, 2)
            self.assertEqual(sent, [])
            self.assertIn("Total API requests: 0", out)
            self.assertIn("SARASHINA_API_KEY", err)
        self.assertIn("Environment: Local", out)

    def test_does_not_modify_repository_files(self):
        targets = [p for p in (ROOT / "data" / "weeks.json", *sorted((ROOT / "weekly").glob("*/*.md")),
                               *sorted((ROOT / "logs").glob("*/*.md"))) if p.exists()]
        before = {p: p.read_bytes() for p in targets}
        weekly_files = sorted((ROOT / "weekly").rglob("*")) if (ROOT / "weekly").exists() else []
        self.go(http_error(403, FORBIDDEN), ok_structured())
        self.assertEqual({p: p.read_bytes() for p in targets}, before)
        self.assertEqual(sorted((ROOT / "weekly").rglob("*")) if (ROOT / "weekly").exists() else [], weekly_files)


class Isolation(unittest.TestCase):
    """通常の週刊生成・スケジュール実行に診断が混入しないこと。"""

    def test_weekly_does_not_use_diagnostics(self):
        self.assertNotIn("sarashina_diag", (ROOT / "scripts" / "weekly.py").read_text(encoding="utf-8"))
        self.assertNotIn("sarashina_diag", (ROOT / ".github/workflows/weekly.yml").read_text(encoding="utf-8"))
        self.assertNotIn("sarashina_diag", (ROOT / ".github/workflows/collect.yml").read_text(encoding="utf-8"))

    def test_diagnostics_workflow_is_manual_and_read_only(self):
        import yaml
        text = (ROOT / ".github/workflows/sarashina-diagnostics.yml").read_text(encoding="utf-8")
        wf = yaml.safe_load(text)
        self.assertEqual(set(wf.get(True, wf.get("on"))), {"workflow_dispatch"})  # scheduleなし
        self.assertEqual(wf["permissions"], {"contents": "read"})
        self.assertIn("python scripts/sarashina_diag.py", text)
        self.assertNotIn("git push", text)
        self.assertNotIn("git commit", text)
        self.assertNotIn("BRAVE_API_KEY", text)
        self.assertNotIn("JEV_API_KEY", text)


if __name__ == "__main__":
    unittest.main()
