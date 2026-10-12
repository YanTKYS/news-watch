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
import topic_dedup  # noqa: E402

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
        self.months = d / "data" / "months.json"
        self.rejected = d / "data" / "jev-rejected.json"

    def run_collect(self, responder, today="2026-09-30", env=None, config=None):
        env = {"BRAVE_API_KEY": SECRET} if env is None else env
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(collect.urllib.request, "urlopen", side_effect=responder) as m, \
                redirect_stdout(out), redirect_stderr(err):
            code = collect.run(config or self.config, self.seen, self.logs, self.months, self.rejected,
                               today, env, sleep=lambda s: None)
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


class MonthsIndex(Base):
    def touch_log(self, rel):
        p = self.logs / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# x\n", encoding="utf-8")

    def months_value(self):
        return json.loads(self.months.read_text(encoding="utf-8"))

    def test_sorted_newest_first_across_years(self):
        for rel in ("2026/2026-09.md", "2026/2026-10.md", "2027/2027-01.md", "2026/2026-02.md"):
            self.touch_log(rel)
        self.assertEqual(collect.list_months(self.logs), ["2027-01", "2026-10", "2026-09", "2026-02"])

    def test_ignores_non_matching_files(self):
        self.touch_log("2026/2026-10.md")
        for rel in ("2026/2026-13.md", "2026/2026-00.md", "2026/notes.md", "2026/2026-9.md",
                    "2026/2026-10.txt", "2026/2026-10.md.bak", "2025/2026-11.md", "2026-11.md",
                    "2026/sub/2026-12.md", "2026/.gitkeep"):
            self.touch_log(rel)
        self.assertEqual(collect.list_months(self.logs), ["2026-10"])

    def test_missing_logs_dir_and_no_duplicates(self):
        self.assertEqual(collect.list_months(self.logs), [])
        self.touch_log("2026/2026-10.md")
        self.assertEqual(collect.list_months(self.logs), collect.list_months(self.logs))
        self.assertEqual(collect.list_months(self.logs).count("2026-10"), 1)

    def test_update_months_writes_only_on_change(self):
        self.touch_log("2026/2026-09.md")
        self.assertTrue(collect.update_months(self.months, self.logs))
        self.assertEqual(self.months_value(), ["2026-09"])
        mtime = self.months.stat().st_mtime_ns
        self.assertFalse(collect.update_months(self.months, self.logs))
        self.assertEqual(self.months.stat().st_mtime_ns, mtime)
        self.touch_log("2026/2026-10.md")
        self.assertTrue(collect.update_months(self.months, self.logs))
        self.assertEqual(self.months_value(), ["2026-10", "2026-09"])

    def test_run_creates_months_and_second_run_same_month_keeps_it(self):
        n = iter(range(100))
        responder = lambda req, timeout=None: FakeResponse(api_payload(result(f"https://e.example.jp/{next(n)}")))
        self.run_collect(responder, today="2026-10-01")
        self.assertEqual(self.months_value(), ["2026-10"])
        mtime = self.months.stat().st_mtime_ns
        self.run_collect(responder, today="2026-10-02")  # 同じ月への追記では書き換えない
        self.assertEqual(self.months.stat().st_mtime_ns, mtime)
        self.run_collect(responder, today="2026-11-01")
        self.assertEqual(self.months_value(), ["2026-11", "2026-10"])

    def test_no_new_items_does_not_create_or_touch_months(self):
        responder = lambda req, timeout=None: FakeResponse(api_payload(result("https://a.example.jp/x")))
        self.run_collect(responder)
        before = self.months.read_text(encoding="utf-8")
        mtime = self.months.stat().st_mtime_ns
        self.run_collect(responder, today="2026-11-01")  # 全件既知 → 新着0件
        self.assertEqual(self.months.read_text(encoding="utf-8"), before)
        self.assertEqual(self.months.stat().st_mtime_ns, mtime)
        # 新着0件の初回実行ではmonths.jsonも作らない
        self.months.unlink()
        self.run_collect(responder, today="2026-11-01")
        self.assertFalse(self.months.exists())

    def test_shipped_months_json_matches_logs(self):
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(json.loads((root / "data" / "months.json").read_text(encoding="utf-8")),
                         collect.list_months(root / "logs"))


JEV_SECRET = "dummy-jev-key-for-tests"  # 実キーではないテスト用ダミー値


def jev_payload(noul):
    return {"model": "typesafe/jev-1.13", "answers": {"relevant": {"type": "noul", "noul": noul}},
            "usage": {"input_tokens": 100, "output_tokens": 5}}


class JevFilter(Base):
    """Jev APIは常にmock。実APIは呼ばない。"""

    def setUp(self):
        super().setUp()
        self.brave_calls, self.jev_reqs = [], []
        self.jev_scores = {}  # URL（Title行に含めたもの）ではなく、Titleをキーにスコアを返す
        self.brave_results = lambda query: []
        self.jev_behavior = None  # callable(req, n) -> FakeResponse / raise

    def responder(self, req, timeout=None):
        if req.full_url.startswith(collect.JEV_ENDPOINT):
            self.jev_reqs.append(req)
            if self.jev_behavior:
                return self.jev_behavior(req, len(self.jev_reqs))
            title = json.loads(req.data)["state"].split("\n")[0].removeprefix("Title: ")
            return FakeResponse(jev_payload(self.jev_scores.get(title, 0.9)))
        self.brave_calls.append(req.full_url)
        query = collect.urllib.parse.parse_qs(collect.urllib.parse.urlsplit(req.full_url).query)["q"][0]
        return FakeResponse(api_payload(*self.brave_results(query)))

    def go(self, jev_key=JEV_SECRET, **kw):
        env = {"BRAVE_API_KEY": SECRET}
        if jev_key:
            env["JEV_API_KEY"] = jev_key
        return self.run_collect(self.responder, env=env, **kw)

    def saved_urls(self):
        return set(json.loads(self.seen.read_text(encoding="utf-8"))) if self.seen.exists() else set()

    # --- 利用量 ---
    def test_excluded_and_seen_urls_never_reach_jev(self):
        self.config.write_text(CONFIG + "exclude_domains: [wikipedia.org]\n"
                               "exclude_url_patterns: [iza.ne.jp/pressrelease/prtimes/]\n", encoding="utf-8")
        self.seen.parent.mkdir(parents=True)
        self.seen.write_text(json.dumps({"https://known.example.jp/a": {"first_seen": "2026-09-01"}}), encoding="utf-8")
        self.brave_results = lambda q: [
            result("https://ja.wikipedia.org/wiki/x", "wiki"),
            result("https://www.iza.ne.jp/pressrelease/prtimes/1", "repost"),
            result("https://known.example.jp/a", "known"),
            result("https://new.example.jp/a", "new"),
        ]
        code, _, out, _ = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.jev_reqs), 1)  # ユニークな新規候補のみ
        self.assertIn("Title: new\n", json.loads(self.jev_reqs[0].data)["state"])
        for line in ["Jev candidates: 1", "Jev requests: 1", "Jev relevant: 1", "Jev irrelevant: 0", "Jev skipped/fallback: 0"]:
            self.assertIn(line, out)

    def test_same_url_from_many_queries_calls_jev_once(self):
        self.config.write_text('themes:\n  - name: t\n    queries: [q1, q2, q3, q4, q5]\n', encoding="utf-8")
        self.brave_results = lambda q: [result("https://same.example.jp/a", "same")]
        code, _, out, _ = self.go()
        self.assertEqual((code, len(self.brave_calls), len(self.jev_reqs)), (0, 5, 1))
        state = json.loads(self.jev_reqs[0].data)["state"]
        self.assertIn("Search queries: q1 / q2 / q3 / q4 / q5", state)  # Queriesは統合されて渡る
        self.assertIn("- Queries: q1 / q2 / q3 / q4 / q5", self.log_text())

    def test_hard_limit_and_fail_open_after_limit(self):
        # 1クエリ10件まで取得できるので、3クエリで30候補（上限20を超える）を作る
        self.config.write_text('themes:\n  - name: t\n    queries: [qa, qb, qc]\n', encoding="utf-8")
        self.brave_results = lambda q: [result(f"https://e.example.jp/{q}/{i}", f"{q}{i}") for i in range(10)]
        self.jev_scores = {f"qc{i}": 0.0 for i in range(10)}  # 上限後の候補（非関連相当）も保存される
        code, _, out, _ = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.jev_reqs), collect.MAX_JEV_REQUESTS)
        self.assertEqual(len(self.saved_urls()), 30)  # 20件判定(関連) + 上限到達の10件はfail-openで保存
        for line in ["Jev candidates: 30", "Jev requests: 20", "Jev relevant: 20", "Jev skipped/fallback: 10", "new items: 30"]:
            self.assertIn(line, out)

    def test_invariant_requests_le_candidates_le_max(self):
        # 3クエリ×(共通2件+固有2件)=ユニーク候補8件（共通分は統合される）
        self.brave_results = lambda q: [result(f"https://e.example.jp/common/{i}", f"c{i}") for i in range(2)] + \
                                       [result(f"https://e.example.jp/{q}/{i}", f"{q}{i}") for i in range(2)]
        _, _, out, _ = self.go()
        self.assertEqual(len(self.jev_reqs), 8)
        self.assertLessEqual(len(self.jev_reqs), 8)  # Jev requests <= ユニーク新規候補
        self.assertLessEqual(len(self.jev_reqs), collect.MAX_JEV_REQUESTS)
        self.assertIn("Jev candidates: 8", out)
        self.assertEqual(len(self.brave_calls), 3)

    # --- 判定 ---
    def test_threshold_and_irrelevant_not_saved_anywhere(self):
        self.brave_results = lambda q: [result("https://ok.example.jp/a", "ok"),
                                        result("https://edge.example.jp/a", "edge"),
                                        result("https://bad.example.jp/a", "bad")]
        self.jev_scores = {"ok": 0.95, "edge": collect.JEV_RELEVANCE_THRESHOLD, "bad": 0.29}
        code, _, out, _ = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(collect.JEV_RELEVANCE_THRESHOLD, 0.30)
        text = self.log_text()
        self.assertIn("https://ok.example.jp/a", text)
        self.assertIn("https://edge.example.jp/a", text)  # noul >= 0.30 は関連
        self.assertNotIn("bad.example.jp", text)
        self.assertEqual(self.saved_urls(), {"https://ok.example.jp/a", "https://edge.example.jp/a"})
        for line in ["Jev relevant: 2", "Jev irrelevant: 1", "new items: 2"]:
            self.assertIn(line, out)

    def test_all_irrelevant_writes_nothing(self):
        self.brave_results = lambda q: [result("https://bad.example.jp/a", "bad")]
        self.jev_scores = {"bad": 0.0}
        code, _, out, _ = self.go()
        self.assertEqual(code, 0)
        self.assertFalse(self.logs.exists() or self.seen.exists() or self.months.exists())
        self.assertIn("new items: 0", out)

    def test_request_shape_and_secret_handling(self):
        self.brave_results = lambda q: [result("https://a.example.jp/x", "T", "説明文")]
        self.go()
        req = self.jev_reqs[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertNotIn(JEV_SECRET, req.full_url)
        self.assertEqual(req.get_header("Authorization"), f"Bearer {JEV_SECRET}")
        body = json.loads(req.data)
        self.assertEqual(body["model"], collect.JEV_MODEL)
        self.assertEqual(collect.JEV_MODEL, "jev-latest")  # TypeSafe直API向け（typesafe/ 接頭辞はOpenRouter用）
        self.assertEqual(list(body["questions"]), ["relevant"])  # 1記事につきNoul 1問
        self.assertEqual(body["questions"]["relevant"]["type"], "noul")
        self.assertEqual(body["state"], "Title: T\nSource: a.example.jp\nDescription: 説明文\nSearch queries: 自治体 DX / 自治体 生成AI / 市役所 生成AI")
        self.assertNotIn(JEV_SECRET, req.data.decode("utf-8"))
        self.assertNotIn(SECRET, req.data.decode("utf-8"))

    # --- 障害時はfail-open ---
    def test_missing_key_skips_api_and_keeps_items(self):
        self.brave_results = lambda q: [result("https://a.example.jp/x", "a")]
        code, _, out, _ = self.go(jev_key=None)
        self.assertEqual(code, 0)
        self.assertEqual(self.jev_reqs, [])
        self.assertIn("JEV_API_KEY is not configured", out)
        self.assertIn("https://a.example.jp/x", self.log_text())
        self.assertIn("Jev requests: 0", out)
        self.assertIn("Jev skipped/fallback: 1", out)

    def test_failures_keep_items_without_retry(self):
        cases = {
            "timeout": lambda req, n: (_ for _ in ()).throw(TimeoutError(JEV_SECRET)),
            "http429": lambda req, n: (_ for _ in ()).throw(urllib.error.HTTPError(req.full_url, 429, JEV_SECRET, {}, io.BytesIO(JEV_SECRET.encode()))),
            "http500": lambda req, n: (_ for _ in ()).throw(urllib.error.HTTPError(req.full_url, 500, JEV_SECRET, {}, io.BytesIO(b""))),
            "badjson": lambda req, n: type("R", (FakeResponse,), {"read": lambda self, n=-1: b"{not json"})({}),
            "no_noul": lambda req, n: FakeResponse({"answers": {"relevant": {"type": "noul"}}}),
            "no_answers": lambda req, n: FakeResponse({"model": "x"}),
            "non_numeric": lambda req, n: FakeResponse({"answers": {"relevant": {"type": "noul", "noul": "high"}}}),
            "bool": lambda req, n: FakeResponse({"answers": {"relevant": {"type": "noul", "noul": True}}}),
            "out_of_range": lambda req, n: FakeResponse(jev_payload(1.5)),
        }
        for name, behavior in cases.items():
            with self.subTest(name):
                for p in (self.logs, self.seen, self.months):
                    if p.is_dir():
                        import shutil; shutil.rmtree(p)
                    elif p.exists():
                        p.unlink()
                self.jev_reqs.clear()
                self.jev_behavior = behavior
                self.brave_results = lambda q: [result("https://a.example.jp/x", "a")]
                code, _, out, err = self.go()
                self.assertEqual(code, 0)
                self.assertEqual(len(self.jev_reqs), 1)  # リトライなし
                self.assertIn("https://a.example.jp/x", self.log_text())
                self.assertEqual(self.saved_urls(), {"https://a.example.jp/x"})
                self.assertIn("Jev skipped/fallback: 1", out)
                self.assertNotIn(JEV_SECRET, out + err + self.log_text())

    def test_one_failure_does_not_stop_others(self):
        self.brave_results = lambda q: [result("https://a.example.jp/1", "a1"), result("https://a.example.jp/2", "a2"),
                                        result("https://a.example.jp/3", "a3")]
        self.jev_scores = {"a3": 0.0}

        def behavior(req, n):
            if n == 1:
                raise urllib.error.URLError("down")
            title = json.loads(req.data)["state"].split("\n")[0].removeprefix("Title: ")
            return FakeResponse(jev_payload(self.jev_scores.get(title, 0.9)))

        self.jev_behavior = behavior
        _, _, out, _ = self.go()
        self.assertEqual(len(self.jev_reqs), 3)
        self.assertEqual(self.saved_urls(), {"https://a.example.jp/1", "https://a.example.jp/2"})
        for line in ["Jev relevant: 1", "Jev irrelevant: 1", "Jev skipped/fallback: 1"]:
            self.assertIn(line, out)

    def test_brave_request_count_unchanged_by_jev(self):
        self.brave_results = lambda q: [result(f"https://e.example.jp/{q}", q)]
        self.go()
        self.assertEqual(len(self.brave_calls), 3)  # 1検索語=1リクエスト
        self.assertTrue(all(u.startswith(collect.API_ENDPOINT) for u in self.brave_calls))

    # --- 非関連キャッシュ（jev-rejected.json, 7日） ---
    def rejected_value(self):
        return json.loads(self.rejected.read_text(encoding="utf-8"))

    def write_rejected(self, mapping):
        self.rejected.parent.mkdir(parents=True, exist_ok=True)
        self.rejected.write_text(json.dumps({u: {"rejected_at": d} for u, d in mapping.items()}), encoding="utf-8")

    def test_irrelevant_is_cached_not_seen_and_not_rejudged_next_day(self):
        self.brave_results = lambda q: [result("https://bad.example.jp/a", "bad"), result("https://ok.example.jp/a", "ok")]
        self.jev_scores = {"bad": 0.0}
        self.go(today="2026-10-01")
        self.assertEqual(self.rejected_value(), {"https://bad.example.jp/a": {"rejected_at": "2026-10-01"}})
        self.assertEqual(self.saved_urls(), {"https://ok.example.jp/a"})  # seen.jsonには入れない
        self.assertEqual(len(self.jev_reqs), 2)
        self.jev_reqs.clear()
        _, _, out, _ = self.go(today="2026-10-02")
        self.assertEqual(self.jev_reqs, [])  # 非関連はキャッシュ、関連はseenで、Jevは呼ばれない
        self.assertIn("Jev cached rejects (not re-judged): 1", out)
        self.assertIn("Jev requests: 0", out)
        self.assertNotIn("bad.example.jp", self.log_text("2026/2026-10.md"))

    def test_cache_ttl_boundary(self):
        self.brave_results = lambda q: [result("https://bad.example.jp/a", "bad")]
        self.jev_scores = {"bad": 0.0}
        self.write_rejected({"https://bad.example.jp/a": "2026-10-01"})
        self.go(today="2026-10-08")  # ちょうど7日 → キャッシュ有効
        self.assertEqual(self.jev_reqs, [])
        self.go(today="2026-10-09")  # 8日 → 期限切れ。再判定される
        self.assertEqual(len(self.jev_reqs), 1)
        self.assertEqual(self.rejected_value()["https://bad.example.jp/a"], {"rejected_at": "2026-10-09"})

    def test_expired_cache_can_turn_relevant_and_gets_saved(self):
        self.brave_results = lambda q: [result("https://x.example.jp/a", "x")]
        self.write_rejected({"https://x.example.jp/a": "2026-09-01"})
        self.go(today="2026-10-01")
        self.assertEqual(self.saved_urls(), {"https://x.example.jp/a"})

    def test_cache_hit_before_merge_means_no_jev_and_no_save(self):
        self.config.write_text('themes:\n  - name: t\n    queries: [q1, q2]\n', encoding="utf-8")
        self.brave_results = lambda q: [result("https://bad.example.jp/a", "bad"), result("https://new.example.jp/a", "new")]
        self.write_rejected({"https://bad.example.jp/a": "2026-10-01"})
        _, _, out, _ = self.go(today="2026-10-02")
        self.assertEqual(len(self.jev_reqs), 1)  # newのみ（2クエリ分は統合）
        self.assertIn("Jev candidates: 1", out)
        self.assertIn("Jev cached rejects (not re-judged): 1", out)  # ユニークURL数
        self.assertNotIn("bad.example.jp", self.log_text("2026/2026-10.md"))
        self.assertNotIn("https://bad.example.jp/a", self.saved_urls())

    def test_cache_written_only_when_new_rejections_and_pruned_then(self):
        self.brave_results = lambda q: [result("https://ok.example.jp/a", "ok")]
        self.write_rejected({"https://old.example.jp/a": "2026-09-01"})
        before = self.rejected.read_text(encoding="utf-8")
        self.go(today="2026-10-01")  # 新たな非関連なし → 書き換えない（期限切れ掃除もしない）
        self.assertEqual(self.rejected.read_text(encoding="utf-8"), before)
        self.brave_results = lambda q: [result("https://bad.example.jp/a", "bad")]
        self.jev_scores = {"bad": 0.0}
        self.go(today="2026-10-02")  # 新たな非関連あり → 書き込み、期限切れは掃除
        self.assertEqual(set(self.rejected_value()), {"https://bad.example.jp/a"})

    def test_failures_and_fail_open_never_enter_cache(self):
        self.brave_results = lambda q: [result("https://a.example.jp/x", "a")]
        self.jev_behavior = lambda req, n: (_ for _ in ()).throw(urllib.error.URLError("down"))
        self.go()
        self.assertFalse(self.rejected.exists())
        self.jev_behavior = None
        self.go(jev_key=None, today="2026-10-02")
        self.assertFalse(self.rejected.exists())

    def test_cache_not_applied_without_jev_key(self):
        self.brave_results = lambda q: [result("https://bad.example.jp/a", "bad")]
        self.write_rejected({"https://bad.example.jp/a": "2026-10-01"})
        self.go(jev_key=None, today="2026-10-02")
        self.assertEqual(self.jev_reqs, [])
        self.assertIn("https://bad.example.jp/a", self.log_text("2026/2026-10.md"))  # Jev無効時は従来どおり保存

    def test_corrupt_or_malformed_cache_is_ignored(self):
        self.brave_results = lambda q: [result("https://a.example.jp/x", "a")]
        for content in ("{broken", "[]", '{"https://a.example.jp/x": {"rejected_at": "yesterday"}}',
                        '{"https://a.example.jp/x": "2026-10-01"}'):
            with self.subTest(content):
                self.rejected.parent.mkdir(parents=True, exist_ok=True)
                self.rejected.write_text(content, encoding="utf-8")
                self.jev_reqs.clear()
                code, _, _, _ = self.go(today="2026-10-02")
                self.assertEqual(code, 0)
                self.assertEqual(len(self.jev_reqs), 1)  # キャッシュは無効扱い
                for p in (self.logs, self.seen):
                    if p.is_dir():
                        import shutil; shutil.rmtree(p)
                    elif p.exists():
                        p.unlink()

    def test_workflow_passes_jev_secret_only_to_collect_step(self):
        import yaml
        root = Path(__file__).resolve().parent.parent
        wf = yaml.safe_load((root / ".github/workflows/collect.yml").read_text(encoding="utf-8"))
        steps = wf["jobs"]["collect"]["steps"]
        with_jev = [st for st in steps if "JEV_API_KEY" in st.get("env", {})]
        self.assertEqual([st["name"] for st in with_jev], ["Collect"])
        commit = next(st for st in steps if st.get("name", "").startswith("Commit"))
        self.assertIn("data/jev-rejected.json", commit["run"])
        # 存在しないパスを git add するとcommit stepが失敗するため、初期ファイルを同梱している
        self.assertTrue((root / "data" / "jev-rejected.json").exists())
        self.assertEqual(with_jev[0]["env"]["JEV_API_KEY"], "${{ secrets.JEV_API_KEY }}")
        self.assertEqual(wf["permissions"], {"contents": "write"})
        self.assertNotIn("secrets", (root / ".github/workflows/test.yml").read_text(encoding="utf-8"))


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


# ---- 同一話題の重複候補（検証フェーズ・観測のみ） -----------------------------------
QOMMONS_ASCII = ("行政向け生成AI「QommonsAI」、J-LISフェア2026でエージェント版と行政専用AIアプリストア「Qommons ONE」を披露 "
                 "- 自治体の定型業務を、職員の承認の基でAIが最後まで進める : - ASCII STARTUP")
QOMMONS_PRTIMES = ("行政向け生成AI「QommonsAI」、J-LISフェア2026でエージェント版と行政専用AIアプリストア「Qommons ONE」を披露 "
                   "— 自治体の定型業務を、職員の承認の基でAIが最後まで進める | Polimill株式会社のプレスリリース")
QOMMONS_CODECAMP = ("行政向け生成AI「QommonsAI」、J-LISフェア2026でエージェント版と行政専用AIアプリストア「Qommons ONE」を披露 "
                    "— 自治体の定型業務を、職員の承認の基でAIが最後まで進める")
QOMMONS_GIFU = "QommonsAI活用研修を岐阜県庁で実施 - 「生成AIを、個人の道具から、組織の知恵へ」庁内文書を活かす本格活用の一歩 : - ASCII STARTUP"
NTT_CODECAMP = "自治体における脱炭素化推進に向け、生成AIを活用した新たなコンサルティング手法（地 方公共団体実行計画区域施策編）を確立"
NTT_PRTIMES = ("自治体における脱炭素化推進に向け、生成AIを活用した新たなコンサルティング手法（地方公共団体実行計画区域施策編）を確立 "
               "| NTT西日本株式会社のプレスリリース")
NTT_DEMPA = "NTT西、生成AIで自治体の脱炭素計画策定を支援 自治体職員の負担軽減へ | 電波新聞デジタル"


def art(title, url=None, retrieved=""):
    return topic_dedup.Article(url=url or f"https://{abs(hash(title)) % 10**8}.example.jp/a", title=title, retrieved=retrieved)


def scored(a, b):
    return topic_dedup.compare(art(a, "https://a.example.jp/1"), art(b, "https://b.example.jp/2"))


class TopicSimilarity(unittest.TestCase):
    def assertDuplicate(self, a, b):
        score = scored(a, b)
        self.assertIsNotNone(score, (a, b))
        self.assertGreaterEqual(score, topic_dedup.TOPIC_SIMILARITY_THRESHOLD, (a, b))

    def assertNotDuplicate(self, a, b):
        score = scored(a, b)
        self.assertTrue(score is None or score < topic_dedup.TOPIC_SIMILARITY_THRESHOLD, (a, b, score))

    def test_same_title_different_url(self):
        self.assertEqual(topic_dedup.compare(art("生成AIチャットボットを全庁で導入", "https://a.example.jp/1"),
                                             art("生成AIチャットボットを全庁で導入", "https://b.example.jp/2")), 1.0)

    def test_same_url_is_not_a_pair(self):
        self.assertIsNone(topic_dedup.compare(art("生成AIチャットボットを全庁で導入", "https://a.example.jp/1"),
                                              art("生成AIチャットボットを全庁で導入", "https://a.example.jp/1")))

    def test_reprint_with_changed_media_name_and_symbols(self):
        # 実ログ: QommonsAIの同一発表（ASCII STARTUP / PR TIMES / CodeCamp Trends）
        self.assertDuplicate(QOMMONS_ASCII, QOMMONS_PRTIMES)
        self.assertDuplicate(QOMMONS_ASCII, QOMMONS_CODECAMP)
        self.assertDuplicate(QOMMONS_PRTIMES, QOMMONS_CODECAMP)
        # 実ログ: NTT西日本のプレスリリース（PR TIMES / CodeCamp Trends。後者は「地 方」と空白混じり）
        self.assertDuplicate(NTT_CODECAMP, NTT_PRTIMES)
        # 実ログ: 同一記事の配信先違い
        base = "グーグル、観光や行政支援など 全国9自治体の生成AI事例を公開"
        self.assertDuplicate(base + " - Impress Watch", base + " | チバテレ＋プラス")
        self.assertDuplicate(base + " - Impress Watch", base + "（Impress Watch）｜ｄメニューニュース")

    def test_japanese_notation_variants(self):
        self.assertDuplicate("株式会社ＴＫＣ、自治体ＤＸ推進フェアへ出展 | 株式会社ＴＫＣのプレスリリース",
                             "TKC、自治体DX推進フェアへ出展 - 産経ニュース")  # 全角英数・法人格・媒体名
        self.assertDuplicate("栃木県真岡市で、生成ＡＩチャットボットの提供を開始！",
                             "栃木県 真岡市で 生成AIチャットボットの提供を開始")  # 全角半角・空白・記号
        self.assertDuplicate("栃木県真岡市で生成AIチャットボットの提供を開始", "真岡市で生成AIチャットボットの提供を開始 - 静岡新聞アットエス")

    def test_normalize_title(self):
        n = topic_dedup.normalize_title
        self.assertEqual(n("ＡＩ活用 ｜ 〇〇新聞"), n("AI活用"))
        self.assertEqual(n("【速報】 生成AI、導入へ！"), "速報生成ai導入へ")
        self.assertEqual(n("A社のプレスリリース"), n("A社"))
        self.assertEqual(n("Ａ株式会社"), n("a"))  # 法人格は無視。大文字小文字も無視

    def test_same_company_different_news_is_not_duplicate(self):
        # 実ログ: 同じQommonsAIでも、フェア出展と岐阜県庁での研修は別の出来事
        self.assertNotDuplicate(QOMMONS_ASCII, QOMMONS_GIFU)
        # 導入発表と研修実施はテンプレートが似ていても統合しない
        a = "Polimill、行政向け生成AI「QommonsAI」の提供を開始"
        b = "Polimill、行政向け生成AI「QommonsAI」の活用研修を実施"
        self.assertGreaterEqual(topic_dedup.similarity(topic_dedup.normalize_title(a), topic_dedup.normalize_title(b)),
                                topic_dedup.TOPIC_SIMILARITY_THRESHOLD)  # 文字列だけなら閾値を超える類似
        self.assertNotDuplicate(a, b)
        # 同じ市・同じ製品でも導入と研修は別
        self.assertNotDuplicate("大分市、生成AIの導入を発表 庁内文書の検索を効率化", "大分市、生成AIの研修を実施 庁内文書の検索を効率化")

    def test_similar_initiative_in_different_municipalities_is_not_duplicate(self):
        a = "栃木県真岡市で、生成AIチャットボットの提供を開始"
        b = "千葉県館山市で、生成AIチャットボットの提供を開始"
        self.assertGreaterEqual(topic_dedup.similarity(topic_dedup.normalize_title(a), topic_dedup.normalize_title(b)),
                                topic_dedup.TOPIC_SIMILARITY_THRESHOLD)
        self.assertNotDuplicate(a, b)

    def test_unrelated_company_mentions_and_same_event_name_are_not_duplicate(self):
        # 実ログ: 同じ展示会名を含むだけの別々の出展案件・告知
        self.assertNotDuplicate("2040年を見据えた自治体DX推進をテーマに、「地方自治情報化推進フェア2026」へ出展",
                                "「地方自治情報化推進フェア2026」いよいよ10/27（火）・10/28（水）、幕張メッセで開催！ - 自治体DX | 行政 ニュース")
        self.assertNotDuplicate("Bot Express、10月20日開催「TOHOKU DX GATEWAY 2026」に青森県むつ市と共同出展",
                                "自治体DX需要の拡大を追い風に、ニーズウェルが「TOHOKU DX GATEWAY 2026」に出展")

    def test_ambiguous_rewrites_are_not_asserted(self):
        # 実ログ: 見出しが書き換えられた記事は、タイトルだけでは断定しない（NTT西日本の電波新聞）
        self.assertNotDuplicate(NTT_DEMPA, NTT_PRTIMES)
        self.assertNotDuplicate("デジタル庁OSS「源内」を活用した生成AIサービスを自治体で初めてLGWAN環境から利用可能に",
                                "デジタル庁OSS「源内」活用の生成AI、大分市がLGWAN環境で自治体初導入")

    def test_short_titles_are_not_compared(self):
        self.assertNotDuplicate("DXアンテナ - Wikipedia", "ダウンタウンDX - Wikipedia")
        self.assertIsNone(scored("デジタル庁", "デジタル庁"))

    def test_place_subset_is_not_a_conflict(self):
        self.assertEqual(topic_dedup.extract_places("栃木県真岡市で生成AI"), {"栃木県", "真岡市"})
        self.assertEqual(topic_dedup.extract_places("実行計画区域施策編と地区と市町村の取組"), set())  # 一般語は自治体名としない

    def test_thresholds_are_tunable_constants(self):
        a = "デジタル庁OSS「源内」を活用した生成AIサービスを自治体で初めてLGWAN環境から利用可能に"
        b = "デジタル庁OSS「源内」活用の生成AI、大分市がLGWAN環境で自治体初導入"
        result = topic_dedup.detect_topic_duplicates([art(a, "https://a.example.jp/1")], [art(b, "https://b.example.jp/1", "2026-10-01")])
        self.assertEqual((result.pair_count, result.near_misses), (0, 1))  # 既定では曖昧なので候補にせず、惜しい組み合わせとして数える
        with mock.patch.object(topic_dedup, "TOPIC_SIMILARITY_THRESHOLD", 0.5):
            result = topic_dedup.detect_topic_duplicates([art(a, "https://a.example.jp/1")], [art(b, "https://b.example.jp/1", "2026-10-01")])
            self.assertEqual((result.pair_count, result.near_misses), (1, 0))
        with mock.patch.object(topic_dedup, "TOPIC_SIMILARITY_THRESHOLD", 1.01):
            result = topic_dedup.detect_topic_duplicates([art(QOMMONS_ASCII)], [art(QOMMONS_PRTIMES)])
            self.assertEqual((result.pair_count, result.near_misses), (0, 1))


class TopicPastLogs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name) / "logs"

    def write_month(self, ym, entries):
        """entries: [(retrieved, title, url)]。実ログと同じ形式で書く。"""
        lines = [f"# {ym}"]
        for day, title, url in entries:
            lines += ["", f"## {day}", "", "### 自治体DX", "", f"#### {title}", "", f"- URL: {url}", f"- Source: x.example.jp",
                      "- Queries: q", f"- Retrieved: {day}", "", "説明文は読まない"]
        path = self.logs / ym[:4] / f"{ym}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def titles(self, today):
        return sorted(a.title for a in topic_dedup.load_recent_articles(self.logs, today))

    def test_parse_reads_only_url_title_retrieved(self):
        path = self.write_month("2026-10", [("2026-10-05", "記事A", "https://a.example.jp/1")])
        before = path.read_text(encoding="utf-8")
        arts = topic_dedup.parse_log_articles(before)
        self.assertEqual(arts, [topic_dedup.Article(url="https://a.example.jp/1", title="記事A", retrieved="2026-10-05")])
        self.assertEqual(path.read_text(encoding="utf-8"), before)  # 読み取り専用

    def test_parse_real_log_format_and_incomplete_entries(self):
        text = "# 2026年10月\n\n## 2026-10-05\n\n### t\n\n#### 欠けた記事\n\n- URL: https://a.example.jp/1\n\n#### 完全な記事\n\n" \
               "- URL: https://a.example.jp/2\n- Retrieved: 2026-10-05\n"
        self.assertEqual([a.title for a in topic_dedup.parse_log_articles(text)], ["完全な記事"])  # Retrievedが無い記事は対象外

    def test_window_boundary_7_days_included_8_excluded(self):
        self.write_month("2026-10", [("2026-10-01", "八日前", "https://a.example.jp/8"), ("2026-10-02", "七日前", "https://a.example.jp/7"),
                                     ("2026-10-09", "当日", "https://a.example.jp/0")])
        self.assertEqual(self.titles("2026-10-09"), ["七日前", "当日"])

    def test_window_spans_month_boundary(self):
        self.write_month("2026-09", [("2026-09-25", "九月の古い記事", "https://a.example.jp/old"),
                                     ("2026-09-28", "九月の記事", "https://a.example.jp/new")])
        self.write_month("2026-10", [("2026-10-02", "十月の記事", "https://a.example.jp/oct")])
        self.assertEqual(self.titles("2026-10-03"), ["九月の記事", "十月の記事"])  # 9/25は8日前なので対象外
        self.assertEqual(topic_dedup.months_in_window("2026-10-03"), ["2026-09", "2026-10"])

    def test_window_spans_year_boundary(self):
        self.write_month("2026-12", [("2026-12-30", "去年の記事", "https://a.example.jp/y")])
        self.write_month("2027-01", [("2027-01-02", "今年の記事", "https://a.example.jp/z")])
        self.assertEqual(self.titles("2027-01-03"), ["今年の記事", "去年の記事"])

    def test_missing_month_files_are_ignored(self):
        self.assertEqual(self.titles("2026-10-03"), [])

    def test_detects_duplicate_against_previous_month_log(self):
        self.write_month("2026-09", [("2026-09-29", QOMMONS_ASCII, "https://ascii.jp/1")])
        past = topic_dedup.load_recent_articles(self.logs, "2026-10-03")
        result = topic_dedup.detect_topic_duplicates([art(QOMMONS_PRTIMES, "https://prtimes.jp/1")], past)
        self.assertEqual((result.candidate_count, result.pair_count), (1, 1))
        self.assertTrue(result.pairs[0].other_is_past)

    def test_articles_older_than_window_are_not_compared(self):
        self.write_month("2026-09", [("2026-09-24", QOMMONS_ASCII, "https://ascii.jp/1")])
        past = topic_dedup.load_recent_articles(self.logs, "2026-10-03")
        result = topic_dedup.detect_topic_duplicates([art(QOMMONS_PRTIMES, "https://prtimes.jp/1")], past)
        self.assertEqual(result.pair_count, 0)


class TopicDetect(unittest.TestCase):
    def test_candidates_and_pairs_are_counted_separately(self):
        past = [art(QOMMONS_ASCII, "https://ascii.jp/1", "2026-10-06"), art(QOMMONS_PRTIMES, "https://prtimes.jp/1", "2026-10-07")]
        new = [art(QOMMONS_CODECAMP, "https://codecamp.jp/1"), art("無関係な自治体の別ニュース記事です", "https://x.example.jp/1")]
        result = topic_dedup.detect_topic_duplicates(new, past)
        self.assertEqual((result.candidate_count, result.pair_count), (1, 2))  # 1記事が2件の過去記事に一致

    def test_within_same_run_each_pair_counted_once(self):
        new = [art(QOMMONS_ASCII, "https://ascii.jp/1"), art(QOMMONS_PRTIMES, "https://prtimes.jp/1"), art(QOMMONS_CODECAMP, "https://codecamp.jp/1")]
        result = topic_dedup.detect_topic_duplicates(new, [])
        self.assertEqual((result.candidate_count, result.pair_count), (2, 3))  # 後から現れた2記事が候補、組み合わせは3通り
        self.assertTrue(all(not p.other_is_past for p in result.pairs))

    def test_does_not_mutate_inputs_and_no_false_positive_on_unrelated(self):
        new = [art("自治体の生成AI活用を支援する新サービスを発表", "https://a.example.jp/1")]
        past = [art("まったく別の防災アプリの配信を開始しました", "https://b.example.jp/1", "2026-10-01")]
        n0, p0 = list(new), list(past)
        result = topic_dedup.detect_topic_duplicates(new, past)
        self.assertEqual((result.pair_count, new, past), (0, n0, p0))

    def test_report_is_limited_and_has_no_description(self):
        new = [art(f"{c}市、生成AIで窓口の待ち時間を短縮する新システムを稼働", f"https://a.example.jp/{c}") for c in "ABCDEFGHIJKLMNOPQRST"]
        past = [art(f"{c}市、生成AIで窓口の待ち時間を短縮する新システムを稼働", f"https://b.example.jp/{c}", "2026-10-01") for c in "ABCDEFGHIJKLMNOPQRST"]
        result = topic_dedup.detect_topic_duplicates(new, past)
        lines = topic_dedup.format_report(result)
        self.assertEqual(lines[0], f"Topic duplicate candidates: {result.candidate_count}")
        self.assertEqual(lines[1], f"Topic duplicate pairs: {result.pair_count}")
        self.assertLessEqual(len(lines), 3 + 2 * topic_dedup.TOPIC_MAX_REPORT_LINES + 1)
        self.assertTrue(any("more pairs omitted" in ln for ln in lines) or result.pair_count <= topic_dedup.TOPIC_MAX_REPORT_LINES)
        self.assertTrue(all(len(ln) < 200 for ln in lines))
        self.assertFalse(any("http" in ln for ln in lines))


class TopicRunIntegration(Base):
    """run() 全体で、検出が観測のみであること（保存内容・API呼び出し数が変わらないこと）を確認する。APIは常にmock。"""

    def setUp(self):
        super().setUp()
        self.brave_calls, self.jev_calls = [], []
        self.titles = {}  # URL -> title

    def responder(self, req, timeout=None):
        if req.full_url.startswith(collect.JEV_ENDPOINT):
            self.jev_calls.append(req.full_url)
            return FakeResponse(jev_payload(0.9))
        self.brave_calls.append(req.full_url)
        return FakeResponse(api_payload(*[result(u, t) for u, t in self.titles.items()]))

    def go(self, today, jev=True):
        env = {"BRAVE_API_KEY": SECRET}
        if jev:
            env["JEV_API_KEY"] = JEV_SECRET
        return self.run_collect(self.responder, today=today, env=env)

    def seed_previous_day(self):
        self.titles = {"https://ascii.jp/1": QOMMONS_ASCII}
        self.go("2026-09-29")
        self.brave_calls.clear(), self.jev_calls.clear()

    def test_duplicates_are_reported_but_all_items_saved(self):
        self.seed_previous_day()
        prev_log = self.log_text("2026/2026-09.md")
        self.titles = {"https://prtimes.jp/1": QOMMONS_PRTIMES, "https://codecamp.jp/1": QOMMONS_CODECAMP,
                       "https://other.example.jp/1": "自治体の防災DXに関する全く別のニュース記事"}
        code, _, out, err = self.go("2026-10-03")  # 月をまたぐ（過去ログは2026-09.md）
        self.assertEqual(code, 0)
        self.assertIn("Topic duplicate candidates: 2\n", out)
        self.assertIn("Topic duplicate pairs: 3\n", out)  # PRTIMES→ASCII, CodeCamp→ASCII, CodeCamp→PRTIMES
        saved = set(json.loads(self.seen.read_text(encoding="utf-8")))
        self.assertEqual(saved, {"https://ascii.jp/1", "https://prtimes.jp/1", "https://codecamp.jp/1", "https://other.example.jp/1"})
        text = self.log_text("2026/2026-10.md")
        for url in ["https://prtimes.jp/1", "https://codecamp.jp/1", "https://other.example.jp/1"]:
            self.assertIn(f"- URL: {url}", text)
        self.assertEqual(self.log_text("2026/2026-09.md"), prev_log)  # 過去記事のMarkdownは変更しない
        self.assertNotIn("説明", out.split("Topic duplicate")[1])  # 説明文は出さない
        self.assertNotIn(SECRET, out + err)

    def test_log_format_and_saved_files_are_identical_with_and_without_detection(self):
        self.seed_previous_day()
        self.titles = {"https://prtimes.jp/1": QOMMONS_PRTIMES, "https://codecamp.jp/1": QOMMONS_CODECAMP}
        d = Path(self.tmp.name)
        snapshot = {p.name: p.read_bytes() for p in d.rglob("*") if p.is_file()}
        self.go("2026-10-03")
        with_detection = (self.log_text("2026/2026-10.md"), self.seen.read_bytes(), self.months.read_bytes())
        # 検出を無効化した場合（従来動作）と、保存内容が一致する
        for p in list(d.rglob("*")):
            if p.is_file() and p.name not in snapshot:
                p.unlink()
        self.seen.write_bytes(snapshot["seen.json"])
        self.months.write_bytes(snapshot["months.json"])
        (self.logs / "2026" / "2026-09.md").write_bytes(snapshot["2026-09.md"])
        with mock.patch.object(collect, "detect_topic_duplicates", return_value=None):
            self.go("2026-10-03")
        without = (self.log_text("2026/2026-10.md"), self.seen.read_bytes(), self.months.read_bytes())
        self.assertEqual(with_detection, without)

    def test_api_request_counts_do_not_change(self):
        self.seed_previous_day()
        self.titles = {"https://prtimes.jp/1": QOMMONS_PRTIMES, "https://codecamp.jp/1": QOMMONS_CODECAMP}
        self.go("2026-10-03")
        self.assertEqual(len(self.brave_calls), 3)  # 検索語数（1検索語=1リクエスト）のまま
        self.assertEqual(len(self.jev_calls), 2)  # ユニークな新規候補数のまま（重複候補でも追加・削減なし）
        self.assertTrue(all(u.startswith(collect.API_ENDPOINT) for u in self.brave_calls))
        self.assertTrue(all(u.startswith(collect.JEV_ENDPOINT) for u in self.jev_calls))

    def test_no_duplicates_prints_zero_counts(self):
        self.titles = {"https://a.example.jp/1": "自治体の防災DXに関する全く別のニュース記事"}
        _, _, out, _ = self.go("2026-10-03")
        self.assertIn("Topic duplicate candidates: 0\n", out)
        self.assertIn("Topic duplicate pairs: 0\n", out)

    def test_works_without_jev_key_and_with_no_new_items(self):
        self.titles = {"https://a.example.jp/1": QOMMONS_ASCII}
        self.go("2026-10-03", jev=False)
        self.assertEqual(self.jev_calls, [])
        _, _, out, _ = self.go("2026-10-04", jev=False)  # 同じURL＝新規なし
        self.assertIn("Topic duplicate candidates: 0\n", out)

    def test_detection_failure_never_blocks_saving(self):
        self.titles = {"https://a.example.jp/1": QOMMONS_ASCII}
        with mock.patch.object(topic_dedup, "load_recent_articles", side_effect=OSError("boom")):
            code, _, out, _ = self.go("2026-10-03")
        self.assertEqual(code, 0)
        self.assertIn("::warning::topic duplicate detection skipped: error=OSError", out)
        self.assertNotIn("boom", out)
        self.assertIn("https://a.example.jp/1", self.seen.read_text(encoding="utf-8"))
        self.assertIn("- URL: https://a.example.jp/1", self.log_text("2026/2026-10.md"))

    def test_same_run_duplicates_are_all_saved(self):
        self.titles = {"https://ascii.jp/1": QOMMONS_ASCII, "https://prtimes.jp/1": QOMMONS_PRTIMES}
        _, _, out, _ = self.go("2026-10-07")
        self.assertIn("Topic duplicate candidates: 1\n", out)
        self.assertEqual(self.log_text("2026/2026-10.md").count("#### "), 2)
        self.assertEqual(len(json.loads(self.seen.read_text(encoding="utf-8"))), 2)


if __name__ == "__main__":
    unittest.main()
