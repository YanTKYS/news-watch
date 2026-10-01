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


DOMAINS = ["wikipedia.org", "headtopics.com", "topics.smt.docomo.ne.jp"]
PATTERNS = ["iza.ne.jp/pressrelease/prtimes/", "excite.co.jp/news/article/Prtimes_"]


class ExcludeFilter(Base):
    def test_domain_matching_respects_boundaries_and_case(self):
        ex = lambda u: collect.is_excluded(collect.normalize_url(u), DOMAINS, [])
        self.assertTrue(ex("https://wikipedia.org/wiki/x"))
        self.assertTrue(ex("https://ja.wikipedia.org/wiki/x"))
        self.assertTrue(ex("https://EN.Wikipedia.ORG/wiki/x"))
        self.assertTrue(ex("https://topics.smt.docomo.ne.jp/article/1"))
        self.assertFalse(ex("https://fakewikipedia.org/wiki/x"))
        self.assertFalse(ex("https://wikipedia.org.example.jp/x"))
        self.assertFalse(ex("https://www.docomo.ne.jp/x"))
        self.assertFalse(ex("https://example.jp/wikipedia.org"))  # パスは対象外
        self.assertTrue(collect.is_excluded("https://ja.wikipedia.org/x", ["WikiPedia.org".lower()], []))

    def test_url_patterns_only_exclude_reposts(self):
        ex = lambda u: collect.is_excluded(collect.normalize_url(u), [], PATTERNS)
        self.assertTrue(ex("https://www.iza.ne.jp/pressrelease/prtimes/abc123"))
        self.assertTrue(ex("https://WWW.IZA.NE.JP/pressrelease/prtimes/abc123"))
        self.assertTrue(ex("https://www.excite.co.jp/news/article/Prtimes_2026-10-01_123"))
        self.assertFalse(ex("https://www.iza.ne.jp/news/abc123"))
        self.assertFalse(ex("https://www.excite.co.jp/news/article/Mynavi_123"))
        self.assertFalse(ex("https://prtimes.jp/main/html/rd/p/000000001.000000001.html"))

    def test_pattern_host_is_lowercased_in_config(self):
        self.config.write_text(CONFIG + 'exclude_url_patterns:\n  - "IZA.ne.jp/Pressrelease/"\n', encoding="utf-8")
        cfg = collect.load_config(self.config)
        self.assertEqual(cfg.exclude_url_patterns, ["iza.ne.jp/Pressrelease/"])

    def test_request_query_has_not_site_and_no_key_in_url(self):
        req = collect.build_request('"自治体DX"', SECRET, DOMAINS)
        q = collect.urllib.parse.parse_qs(collect.urllib.parse.urlsplit(req.full_url).query)["q"][0]
        self.assertEqual(q, '"自治体DX" NOT site:wikipedia.org NOT site:headtopics.com NOT site:topics.smt.docomo.ne.jp')
        self.assertNotIn(SECRET, req.full_url)
        self.assertNotIn("offset", req.full_url)

    def test_filtered_results_not_saved_and_counted_one_request_per_query(self):
        urls = [
            "https://ja.wikipedia.org/wiki/x",
            "https://www.iza.ne.jp/pressrelease/prtimes/abc",
            "https://www.iza.ne.jp/news/ok",
            "https://prtimes.jp/main/html/rd/p/1.html",
        ]
        self.config.write_text(CONFIG + "exclude_domains: [wikipedia.org]\n"
                               "exclude_url_patterns: [iza.ne.jp/pressrelease/prtimes/]\n", encoding="utf-8")
        sent = []

        def responder(req, timeout=None):
            sent.append(req.full_url)
            return FakeResponse(api_payload(*[result(u) for u in urls]))

        code, m, out, _ = self.run_collect(responder)
        self.assertEqual(code, 0)
        self.assertEqual(m.call_count, 3)  # 1検索語 = 1リクエスト（補充検索なし）
        self.assertTrue(all("NOT+site%3Awikipedia.org" in u for u in sent))
        text = self.log_text()
        self.assertIn("https://www.iza.ne.jp/news/ok", text)
        self.assertIn("https://prtimes.jp/main/html/rd/p/1.html", text)
        self.assertNotIn("wikipedia.org", text)
        self.assertNotIn("pressrelease/prtimes", text)
        self.assertNotIn("NOT site:", text)  # 利用者向けログには元の検索語のみ
        self.assertIn("- Queries: 自治体 DX", text)
        seen = json.loads(self.seen.read_text(encoding="utf-8"))
        self.assertEqual(set(seen), {"https://www.iza.ne.jp/news/ok", "https://prtimes.jp/main/html/rd/p/1.html"})
        for line in ["results received: 12", "filtered items: 6", "new items: 2", "duplicate items: 4"]:
            self.assertIn(line, out)

    def test_invalid_exclude_config_fails_without_api_call(self):
        bad = [
            "exclude_domains: wikipedia.org\n",
            "exclude_domains: []\nexclude_url_patterns: x\n",
            "exclude_domains: ['']\n",
            "exclude_domains: ['https://wikipedia.org']\n",
            "exclude_domains: ['wikipedia.org/wiki']\n",
            "exclude_domains: ['not a domain']\n",
            "exclude_domains: ['localhost']\n",
            "exclude_domains: [123]\n",
            "exclude_domains:\n",
            "exclude_url_patterns: ['']\n",
            "exclude_url_patterns: [5]\n",
            "exclude_domains: [%s]\n" % ", ".join(f"d{i}.example.com" for i in range(40)),  # 検索語長の上限超過
        ]
        for extra in bad:
            self.config.write_text(CONFIG + extra, encoding="utf-8")
            code, m, _, _ = self.run_collect(lambda *a, **k: self.fail("called"))
            self.assertEqual(code, 2, extra)
            m.assert_not_called()

    def test_exclude_settings_are_optional(self):
        code, m, out, _ = self.run_collect(lambda *a, **k: FakeResponse(api_payload(result("https://a.example.jp/x"))))
        self.assertEqual(code, 0)
        self.assertIn("filtered items: 0", out)


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
        config = collect.load_config(self.root / "config" / "queries.yml")
        queries = [q for t in config.themes for q in t.queries]
        self.assertEqual(len(queries), 5)
        self.assertEqual(queries[0], '"自治体DX"')  # 引用符付きの検索語がそのまま読み込まれる
        self.assertEqual(queries[1], '"自治体" "デジタル化"')
        self.assertTrue(all('"' in q for q in queries))
        self.assertNotIn("自治体 ChatGPT", queries)
        self.assertEqual(config.exclude_domains, ["wikipedia.org", "headtopics.com", "topics.smt.docomo.ne.jp"])
        self.assertEqual(config.exclude_url_patterns,
                         ["iza.ne.jp/pressrelease/prtimes/", "excite.co.jp/news/article/Prtimes_"])
        # PR TIMES本体や他の媒体はドメイン単位では除外しない
        for host in ("prtimes.jp", "www.iza.ne.jp", "www.excite.co.jp", "news.yahoo.co.jp", "note.com"):
            self.assertFalse(collect.is_excluded(f"https://{host}/a", config.exclude_domains, []), host)

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
