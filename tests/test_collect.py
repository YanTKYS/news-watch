import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import collect  # noqa: E402

SECRET = "dummy-key-for-tests"  # 実キーではないテスト用ダミー値


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def api_payload(*results):
    return {"web": {"results": list(results)}}


def result(url, title="タイトル", description="説明", **extra):
    return {"url": url, "title": title, "description": description, **extra}


CONFIG = """
themes:
  - name: 自治体DX
    queries: [自治体 DX]
  - name: 自治体の生成AI活用
    queries: [自治体 生成AI, 市役所 生成AI]
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.config = d / "queries.yml"
        self.config.write_text(CONFIG, encoding="utf-8")
        self.seen = d / "data" / "seen.json"
        self.logs = d / "logs"

    def run_collect(self, responder, today="2026-09-30", env=None, config=None):
        env = {"BRAVE_API_KEY": SECRET} if env is None else env
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(collect.urllib.request, "urlopen", side_effect=responder) as m, \
                redirect_stdout(out), redirect_stderr(err):
            code = collect.run(config or self.config, self.seen, self.logs, today, env, sleep=lambda s: None)
        return code, m, out.getvalue(), err.getvalue()

    def log_text(self, ym="2026/2026-09.md"):
        return (self.logs / ym).read_text(encoding="utf-8")


class UnitTests(unittest.TestCase):
    def test_normalize_url(self):
        n = collect.normalize_url
        self.assertEqual(n("https://Example.JP/a/b/#frag"), "https://example.jp/a/b")
        self.assertEqual(n("https://example.jp/a?utm_source=x&id=1&fbclid=z"), "https://example.jp/a?id=1")
        self.assertEqual(n("https://example.jp/"), "https://example.jp/")
        self.assertEqual(n("https://example.jp:443/a"), "https://example.jp/a")
        self.assertEqual(n("https://example.jp/a?id=1"), "https://example.jp/a?id=1")
        self.assertNotEqual(n("https://example.jp/a?id=1"), n("https://example.jp/a?id=2"))
        self.assertIsNone(n("javascript:alert(1)"))
        self.assertIsNone(n("ftp://example.jp/a"))

    def test_clean_text_and_published(self):
        self.assertEqual(collect.clean_text("<strong>自治体</strong> &amp; DX\n導入"), "自治体 & DX 導入")
        self.assertEqual(collect.clean_text("## 見出し風"), "見出し風")
        self.assertEqual(collect.parse_published("2026-09-29T01:02:03"), "2026-09-29")
        self.assertIsNone(collect.parse_published("3 days ago"))
        self.assertIsNone(collect.parse_published(None))

    def test_request_has_no_key_in_url_and_bounded_count(self):
        req = collect.build_request("自治体 DX", SECRET)
        self.assertNotIn(SECRET, req.full_url)
        self.assertEqual(req.get_header("X-subscription-token"), SECRET)
        self.assertEqual(req.get_header("Accept"), "application/json")
        self.assertIn("count=10", req.full_url)
        self.assertIn("search_lang=ja", req.full_url)
        self.assertNotIn("search_lang=jp", req.full_url)
        self.assertNotIn("offset", req.full_url)
        self.assertLessEqual(collect.RESULT_COUNT, 20)


class ParseAndMerge(Base):
    def test_parse_dedupe_merge_and_markdown(self):
        def responder(req, timeout=None):
            if "%E8%87%AA%E6%B2%BB%E4%BD%93+DX" in req.full_url:  # 自治体 DX
                return FakeResponse(api_payload(
                    result("https://a.example.jp/x/#top", "<b>DX事例</b>", "<strong>説明</strong> です", page_age="2026-09-28T00:00:00"),
                    result("https://b.example.jp/y", "説明なし", ""),
                ))
            if "%E5%B8%82%E5%BD%B9%E6%89%80" in req.full_url:  # 市役所 生成AI
                return FakeResponse(api_payload(result("https://a.example.jp/x?utm_source=z", "DX事例", "別の説明")))
            return FakeResponse(api_payload(result("https://a.example.jp/x", "DX事例", "説明")))

        code, m, out, _ = self.run_collect(responder)
        self.assertEqual(code, 0)
        self.assertEqual(m.call_count, 3)  # 1検索語1リクエスト
        text = self.log_text()
        self.assertTrue(text.startswith("# 2026年9月\n\n## 2026-09-30\n\n### 自治体DX\n"))
        self.assertEqual(text.count("https://a.example.jp/x"), 1)  # 3検索語ヒットでも1件
        self.assertIn("- Queries: 自治体 DX / 自治体 生成AI / 市役所 生成AI", text)
        self.assertIn("- Source: a.example.jp", text)
        self.assertIn("- Published: 2026-09-28", text)
        self.assertIn("- Retrieved: 2026-09-30", text)
        self.assertIn("説明 です", text)
        self.assertNotIn("<strong>", text)
        self.assertNotIn("### 自治体の生成AI活用", text)  # 新規記事がないテーマは出さない
        self.assertEqual(text.count("- Published:"), 1)  # 日付が無い記事には推測で書かない
        self.assertNotIn(SECRET, text + out)
        for line in ["configured queries: 3", "executed queries: 3", "API requests: 3",
                     "results received: 4", "new items: 2", "duplicate items: 2"]:
            self.assertIn(line, out)

    def test_seen_json_roundtrip_and_second_run_is_duplicate(self):
        responder = lambda req, timeout=None: FakeResponse(api_payload(result("https://a.example.jp/x")))
        self.run_collect(responder)
        seen = json.loads(self.seen.read_text(encoding="utf-8"))
        self.assertEqual(seen, {"https://a.example.jp/x": {"first_seen": "2026-09-30"}})
        self.assertEqual(collect.load_seen(self.seen), seen)

    def test_no_new_items_leaves_files_untouched(self):
        responder = lambda req, timeout=None: FakeResponse(api_payload(result("https://a.example.jp/x")))
        self.run_collect(responder)
        log_before, seen_before = self.log_text(), self.seen.read_text(encoding="utf-8")
        mtimes = [p.stat().st_mtime_ns for p in (self.logs / "2026" / "2026-09.md", self.seen)]

        code, _, out, _ = self.run_collect(responder, today="2026-10-01")
        self.assertEqual(code, 0)
        self.assertIn("new items: 0", out)
        self.assertEqual(self.log_text(), log_before)
        self.assertEqual(self.seen.read_text(encoding="utf-8"), seen_before)
        self.assertEqual(mtimes, [p.stat().st_mtime_ns for p in (self.logs / "2026" / "2026-09.md", self.seen)])
        self.assertFalse((self.logs / "2026" / "2026-10.md").exists())

    def test_append_to_existing_day_and_theme_and_new_day(self):
        n = iter(range(100))
        responder = lambda req, timeout=None: FakeResponse(api_payload(result(f"https://e.example.jp/{next(n)}", f"記事{next(n)}")))
        self.run_collect(responder)
        self.run_collect(responder)  # 同日2回目: 既存の日付・テーマ見出しへ追記
        self.run_collect(responder, today="2026-09-30")
        self.run_collect(responder, today="2026-10-01")
        text = self.log_text()
        self.assertEqual(text.count("## 2026-09-30\n"), 1)
        self.assertEqual(text.count("### 自治体DX\n"), 1)
        self.assertEqual(text.count("### 自治体の生成AI活用\n"), 1)
        self.assertEqual(text.count("#### "), 9)
        self.assertNotIn("\n\n\n", text)
        self.assertTrue((self.logs / "2026" / "2026-10.md").read_text(encoding="utf-8").startswith("# 2026年10月\n\n## 2026-10-01"))


class Safety(Base):
    def test_missing_api_key_stops_without_api_call(self):
        for env in ({}, {"BRAVE_API_KEY": ""}, {"BRAVE_API_KEY": "  "}):
            code, m, _, err = self.run_collect(lambda *a, **k: self.fail("called"), env=env)
            self.assertEqual(code, 2)
            m.assert_not_called()
            self.assertIn("BRAVE_API_KEY", err)
        self.assertFalse(self.logs.exists())

    def test_query_limit_exceeded_stops_without_api_call(self):
        qs = "\n".join(f"      - q{i}" for i in range(collect.MAX_QUERIES + 1))
        self.config.write_text(f"themes:\n  - name: t\n    queries:\n{qs}\n", encoding="utf-8")
        code, m, _, err = self.run_collect(lambda *a, **k: self.fail("called"))
        self.assertEqual(code, 2)
        m.assert_not_called()
        self.assertIn("上限", err)

    def test_exactly_max_queries_is_allowed(self):
        qs = "\n".join(f"      - q{i}" for i in range(collect.MAX_QUERIES))
        self.config.write_text(f"themes:\n  - name: t\n    queries:\n{qs}\n", encoding="utf-8")
        code, m, _, _ = self.run_collect(lambda *a, **k: FakeResponse(api_payload()))
        self.assertEqual(code, 0)
        self.assertEqual(m.call_count, collect.MAX_QUERIES)

    def test_bad_configs_fail_without_api_call(self):
        bad = [
            "themes: []\n",
            "themes:\n  - name: t\n    queries: []\n",
            "themes: [unclosed\n",
            "just a string\n",
            "themes:\n  - name: t\n    queries: [a, a]\n",
            "themes:\n  - name: t\n    queries: [1, 2]\n",
        ]
        for content in bad:
            self.config.write_text(content, encoding="utf-8")
            code, m, _, _ = self.run_collect(lambda *a, **k: self.fail("called"))
            self.assertEqual(code, 2, content)
            m.assert_not_called()

    def test_corrupt_seen_json_fails_without_overwrite(self):
        self.seen.parent.mkdir(parents=True)
        self.seen.write_text("{broken", encoding="utf-8")
        code, m, _, _ = self.run_collect(lambda *a, **k: self.fail("called"))
        self.assertEqual(code, 2)
        self.assertEqual(self.seen.read_text(encoding="utf-8"), "{broken")

    def test_api_error_continues_without_retry_and_hides_secret(self):
        calls = []

        def responder(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, f"Too Many {SECRET}", {}, io.BytesIO(SECRET.encode()))
            if len(calls) == 2:
                raise TimeoutError(SECRET)
            return FakeResponse(api_payload(result("https://ok.example.jp/a")))

        code, m, out, err = self.run_collect(responder)
        self.assertEqual(code, 0)
        self.assertEqual(m.call_count, 3)  # 失敗してもリトライせず次の検索語へ
        self.assertIn("HTTP 429", out)
        self.assertIn("TimeoutError", out)
        self.assertIn("failed queries: 2", out)
        self.assertIn("https://ok.example.jp/a", self.log_text())
        self.assertNotIn(SECRET, out + err)

    def test_all_queries_failed_returns_error_and_writes_nothing(self):
        def responder(req, timeout=None):
            raise urllib.error.URLError("down")

        code, m, _, _ = self.run_collect(responder)
        self.assertEqual(code, 1)
        self.assertEqual(m.call_count, 3)
        self.assertFalse(self.logs.exists())
        self.assertFalse(self.seen.exists())

    def test_malformed_response_is_handled(self):
        def responder(req, timeout=None):
            return FakeResponse({"web": {"results": ["x", {"url": 5}, {"url": "ftp://x/y"}, {}]}})

        code, _, out, _ = self.run_collect(responder)
        self.assertEqual(code, 0)
        self.assertIn("new items: 0", out)


class RepoLayout(unittest.TestCase):
    """設定ファイル・ワークフローの安全要件を静的に確認する。"""

    root = Path(__file__).resolve().parent.parent

    def test_shipped_config_is_valid(self):
        themes = collect.load_config(self.root / "config" / "queries.yml")
        queries = [q for t in themes for q in t.queries]
        self.assertEqual(len(queries), 5)
        self.assertEqual(queries[0], '"自治体DX"')  # 引用符付きの検索語がそのまま読み込まれる
        self.assertEqual(queries[1], '"自治体" "デジタル化"')
        self.assertTrue(all('"' in q for q in queries))
        self.assertNotIn("自治体 ChatGPT", queries)

    def test_collect_workflow_triggers_and_permissions(self):
        import yaml
        wf = yaml.safe_load((self.root / ".github/workflows/collect.yml").read_text(encoding="utf-8"))
        triggers = wf.get(True, wf.get("on"))  # PyYAMLは 'on' を True と解釈する
        self.assertEqual(set(triggers), {"schedule", "workflow_dispatch"})
        self.assertEqual(wf["permissions"], {"contents": "write"})

    def test_no_pull_request_target_anywhere(self):
        import yaml
        for p in (self.root / ".github/workflows").glob("*.yml"):
            wf = yaml.safe_load(p.read_text(encoding="utf-8"))
            triggers = wf.get(True, wf.get("on"))
            self.assertNotIn("pull_request_target", triggers, p.name)


if __name__ == "__main__":
    unittest.main()
