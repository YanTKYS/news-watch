import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import monthly  # noqa: E402
import weekly  # noqa: E402
from test_weekly import FakeResponse, completion, entry, http_error, write_month  # noqa: E402

SECRET = "dummy-gemini-key-for-monthly-tests"  # 実キーではないテスト用ダミー値


def trends_review(*source_id_lists, **over):
    data = {
        "summary": "今月は庁内向け生成AI環境の整備と、住民対応への適用が並行して進みました。",
        "trends": [{"title": f"傾向{i}", "body": "複数の事例を踏まえた説明です。", "implication": "実務への示唆です。",
                    "source_ids": list(ids)} for i, ids in enumerate(source_id_lists or (["article-001"],), 1)],
        "outlook": "実証から本格導入へ移行するかを確認する必要があります。",
    }
    data.update(over)
    return data


def weekly_markdown(week, topics):
    """topics: [(title, summary, [(link_title, url)])]。weekly.render_markdown で実際の形式を作る。"""
    by_id, wtopics = {}, []
    for title, summary, links in topics:
        ids = []
        for link_title, url in links:
            aid = f"article-{len(by_id) + 1:03d}"
            by_id[aid] = weekly.Article(title=link_title, url=url, theme="t", source="", published="", retrieved="",
                                        description="")
            ids.append(aid)
        wtopics.append(weekly.Topic(title, summary, "週刊の示唆", ids))
    review = weekly.Review(overview="週の概況（月刊には渡さない）", topics=wtopics, closing="週のまとめ（月刊には渡さない）")
    return weekly.render_markdown(weekly.parse_week(week), review, by_id, len(by_id), date(2026, 10, 20))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.logs = d / "logs"
        self.weekly_dir = d / "reviews" / "weekly"
        self.monthly_dir = d / "reviews" / "monthly"
        self.index = d / "data" / "review-months.json"
        self.requests = []

    def run_monthly(self, responder=None, today="2026-11-01", month=None, force=False, env=None):
        env = {"GEMINI_API_KEY": SECRET} if env is None else env
        responder = responder or (lambda *a, **k: self.fail("API must not be called"))

        def recorder(req, timeout=None):
            self.requests.append((req, timeout))
            return responder(req, timeout=timeout)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(weekly.urllib.request, "urlopen", side_effect=recorder) as m, \
                redirect_stdout(out), redirect_stderr(err):
            code = monthly.run(month_arg=month, force=force, logs_dir=self.logs, weekly_dir=self.weekly_dir,
                               monthly_dir=self.monthly_dir, index_path=self.index, today=today, env=env)
        self.assertLessEqual(m.call_count, 1)  # 月刊1回につき最大1リクエスト
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        return code, m, out.getvalue(), err.getvalue()

    def ok(self, review=None):
        return lambda *a, **k: FakeResponse(completion(review or trends_review()))

    def output(self, label="2026-10"):
        return self.monthly_dir / f"{label}.md"

    def october(self):
        """9/30・10/1・10/15・10/31・11/1 の日刊ログ（対象月の記事は3件）。"""
        write_month(self.logs, "2026-09", [entry("2026-09-30", "九月末の記事", "https://a.example.jp/sep30")])
        write_month(self.logs, "2026-10", [
            entry("2026-10-01", "十月初日の市の発表", "https://a.example.jp/oct01", published="2026-09-20"),
            entry("2026-10-15", "十月中旬の町の取組", "https://a.example.jp/oct15"),
            entry("2026-10-31", "十月末日の県の調査", "https://a.example.jp/oct31", theme="自治体の生成AI活用"),
        ])
        write_month(self.logs, "2026-11", [entry("2026-11-01", "十一月の記事", "https://a.example.jp/nov01")])

    def write_weekly(self, week, topics):
        self.weekly_dir.mkdir(parents=True, exist_ok=True)
        path = self.weekly_dir / f"{week}.md"
        path.write_text(weekly_markdown(week, topics), encoding="utf-8")
        return path

    def sent_prompt(self):
        body = json.loads(self.requests[0][0].data)
        text = body["contents"][0]["parts"][0]["text"]
        topics = json.loads(text.split("<weekly_topics>\n", 1)[1].split("\n</weekly_topics>", 1)[0])
        articles = json.loads(text.split("<articles>\n", 1)[1].split("\n</articles>", 1)[0])
        return body, topics, articles


# ---- 対象月 ---------------------------------------------------------------------
class TargetMonth(unittest.TestCase):
    def check(self, today, start, end):
        m = monthly.previous_month(date.fromisoformat(today))
        self.assertEqual((m.start.isoformat(), m.end.isoformat()), (start, end))

    def test_previous_month_and_year_boundary(self):
        self.check("2026-11-01", "2026-10-01", "2026-10-31")  # 仕様の例
        self.check("2026-11-30", "2026-10-01", "2026-10-31")
        self.check("2027-01-01", "2026-12-01", "2026-12-31")  # 年またぎ
        self.check("2026-02-01", "2026-01-01", "2026-01-31")

    def test_month_lengths(self):
        self.check("2026-03-01", "2026-02-01", "2026-02-28")  # 28日
        self.check("2028-03-01", "2028-02-01", "2028-02-29")  # うるう年 29日
        self.check("2026-05-01", "2026-04-01", "2026-04-30")  # 30日
        self.check("2026-08-01", "2026-07-01", "2026-07-31")  # 31日
        self.assertEqual(monthly.parse_month("2100-02").end, date(2100, 2, 28))  # 100で割り切れる年は平年

    def test_parse_month_rejects_invalid(self):
        for bad in ("2026-13", "2026-00", "2026-1", "26-10", "2026/10", "2026-10; rm -rf /"):
            with self.assertRaises(weekly.WeeklyError, msg=bad):
                monthly.parse_month(bad)

    def test_weeks_overlapping_month(self):
        labels = [w.label for w in monthly.weeks_overlapping(monthly.parse_month("2026-10"))]
        self.assertEqual(labels, ["2026-W40", "2026-W41", "2026-W42", "2026-W43", "2026-W44"])
        labels = [w.label for w in monthly.weeks_overlapping(monthly.parse_month("2027-01"))]
        self.assertEqual(labels[0], "2026-W53")  # 年をまたぐ週を含む
        self.assertEqual(monthly.weekly.period_label(monthly.parse_month("2026-10")), "2026年10月1日〜31日")


# ---- 入力（日刊ログ・週刊レビュー） -------------------------------------------------------------
class Inputs(Base):
    def test_only_articles_retrieved_in_target_month(self):
        self.october()
        code, m, out, err = self.run_monthly(self.ok())
        self.assertEqual(code, 0, err)
        _, _, articles = self.sent_prompt()
        self.assertEqual([a["title"] for a in articles], ["十月初日の市の発表", "十月中旬の町の取組", "十月末日の県の調査"])
        self.assertEqual(articles[0]["published"], "2026-09-20")  # 公開日が対象月外でも取得日で含める
        self.assertIn("Source articles: 3", out)

    def test_month_crossing_weekly_uses_only_in_month_topics(self):
        self.october()
        self.write_weekly("2026-W40", [  # 9/28〜10/4。9月の記事を含むトピックは使わない
            ("九月だけの話題", "九月の要約", [("九月末の記事", "https://a.example.jp/sep30")]),
            ("十月だけの話題", "十月の要約", [("十月初日の市の発表", "https://a.example.jp/oct01")]),
            ("両月にまたがる話題", "混在した要約", [("九月末の記事", "https://a.example.jp/sep30"),
                                                ("十月初日の市の発表", "https://a.example.jp/oct01")]),
        ])
        self.write_weekly("2026-W44", [  # 10/26〜11/1
            ("十一月の話題", "十一月の要約", [("十一月の記事", "https://a.example.jp/nov01")]),
            ("月末の話題", "月末の要約", [("十月末日の県の調査", "https://a.example.jp/oct31")]),
        ])
        code, _, out, err = self.run_monthly(self.ok())
        self.assertEqual(code, 0, err)
        _, topics, articles = self.sent_prompt()
        self.assertEqual([t["title"] for t in topics], ["十月だけの話題", "月末の話題"])
        ids = {a["title"]: a["id"] for a in articles}
        self.assertEqual(topics[0]["source_ids"], [ids["十月初日の市の発表"]])
        self.assertEqual(topics[1]["source_ids"], [ids["十月末日の県の調査"]])
        self.assertEqual(topics[0]["summary"], "十月の要約")
        self.assertEqual(topics[0]["implication"], "週刊の示唆")
        body = json.loads(self.requests[0][0].data)["contents"][0]["parts"][0]["text"]
        for leaked in ("九月", "十一月", "混在した要約", "週の概況", "https://"):
            self.assertNotIn(leaked, body)
        self.assertIn("Weekly topics used: 2 (excluded, citing articles outside the month: 3)", out)
        self.assertIn("missing: 2026-W41, 2026-W42, 2026-W43", out)

    def test_generates_from_daily_logs_when_weekly_reviews_are_missing(self):
        self.october()
        code, _, out, err = self.run_monthly(self.ok())
        self.assertEqual(code, 0, err)
        _, topics, articles = self.sent_prompt()
        self.assertEqual((topics, len(articles)), ([], 3))
        self.assertIn("Weekly reviews: 0 of 5", out)
        self.assertIn("参照した週刊レビュー：0件", self.output().read_text(encoding="utf-8"))

    def test_unreadable_weekly_review_falls_back_to_daily_logs(self):
        self.october()
        self.weekly_dir.mkdir(parents=True)
        (self.weekly_dir / "2026-W42.md").write_bytes(b"\xff\xfe broken")
        code, _, out, err = self.run_monthly(self.ok())
        self.assertEqual(code, 0, err)
        self.assertIn("weekly review ignored: 2026-W42.md", out)

    def test_parse_weekly_review_handles_escapes_and_parentheses(self):
        md = weekly_markdown("2026-W41", [("見出し [PR] *強調*", "要約1\n要約2", [("[速報] 記事", "https://a.example.jp/p_(1)")])])
        (t,) = monthly.parse_weekly_review(md, "2026-W41")
        self.assertEqual((t.title, t.summary, t.implication), ("見出し [PR] *強調*", "要約1 要約2", "週刊の示唆"))
        self.assertEqual(t.urls, ["https://a.example.jp/p_%281%29"])
        arts = [weekly.Article(title="x", url="https://a.example.jp/p_(1)", theme="t", source="", published="",
                               retrieved="2026-10-06", description="")]
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "2026-W41.md").write_text(md, encoding="utf-8")
            used, _, excluded = monthly.load_weekly_topics(Path(d), monthly.parse_month("2026-10"), arts)
        self.assertEqual(([u.urls for u in used], excluded), ([["https://a.example.jp/p_(1)"]], 0))  # 記事側のURLへ戻す

    def test_zero_articles_skips_without_api_call(self):
        write_month(self.logs, "2026-09", [entry("2026-09-30", "九月", "https://a.example.jp/1")])
        code, m, out, _ = self.run_monthly(env={})
        self.assertEqual(code, 0)
        m.assert_not_called()
        self.assertIn("Source articles: 0", out)
        self.assertFalse(self.output().exists())

    def test_selection_prefers_articles_cited_by_weekly_topics(self):
        n = monthly.MAX_ARTICLES + 30
        write_month(self.logs, "2026-10", [entry(f"2026-10-{1 + i % 28:02d}", f"記事{i}番の固有タイトル{'あいうえおかきくけこ'[i % 10] * 3}",
                                                 f"https://a.example.jp/{i}") for i in range(n)])
        arts, _ = weekly.load_week_articles(self.logs, monthly.parse_month("2026-10"))
        cited = [arts[-1].url, arts[-2].url]
        topic = monthly.WeeklyTopic(week="2026-W44", title="t", summary="s", implication="i", urls=cited)
        picked = monthly.select_articles(arts, [topic])
        self.assertEqual(len(picked), monthly.MAX_ARTICLES)
        self.assertTrue(set(cited) <= {a.url for a in picked})
        self.assertEqual(picked, sorted(picked, key=arts.index))


# ---- 実行フロー -------------------------------------------------------------------
class RunFlow(Base):
    def test_generates_markdown_and_index(self):
        self.october()
        usage = {"promptTokenCount": 5000, "candidatesTokenCount": 2000, "thoughtsTokenCount": 900, "totalTokenCount": 7900}
        review = trends_review(["article-001", "article-003"], ["article-002"])
        code, m, out, err = self.run_monthly(lambda *a, **k: FakeResponse(completion(review, usage=usage)))
        self.assertEqual(code, 0, err)
        for line in ("Monthly target: 2026-10 (2026-10-01 - 2026-10-31)", f"Model: {weekly.DEFAULT_MODEL}", "API requests: 1",
                     "Tokens: prompt=5000, output=2000, thoughts=900, total=7900", "Output: reviews/monthly/2026-10.md"):
            self.assertIn(line, out)
        md = self.output().read_text(encoding="utf-8")
        self.assertTrue(md.startswith(f"# {monthly.REVIEW_TITLE}\n\n対象期間：2026年10月1日〜31日\n"))
        for h in ("## 今月の総括", "## 主要トレンド", "### 1. 傾向1", "### 2. 傾向2", "自治体実務への示唆：",
                  "## 翌月以降の注目点", monthly.DISCLAIMER, f"生成モデル：{weekly.DEFAULT_MODEL}"):
            self.assertIn(h, md)
        self.assertIn("- [十月初日の市の発表](https://a.example.jp/oct01)\n- [十月末日の県の調査](https://a.example.jp/oct31)", md)
        self.assertNotIn("sep30", md)
        self.assertEqual(json.loads(self.index.read_text(encoding="utf-8")),
                         [{"month": "2026-10", "start": "2026-10-01", "end": "2026-10-31"}])

    def test_request_reuses_weekly_gemini_format(self):
        self.october()
        self.run_monthly(self.ok())
        (req, timeout), = self.requests
        self.assertEqual(req.full_url, weekly.API_ENDPOINT.format(model=weekly.DEFAULT_MODEL))
        self.assertEqual(req.get_header("X-goog-api-key"), SECRET)
        self.assertNotIn(SECRET, req.full_url)
        self.assertEqual(timeout, weekly.TIMEOUT_SEC)
        body, _, _ = self.sent_prompt()
        self.assertEqual(set(body), {"systemInstruction", "contents", "generationConfig"})  # tools（検索等）なし
        schema = body["generationConfig"]["responseJsonSchema"]
        self.assertEqual(schema["required"], ["summary", "trends", "outlook"])
        self.assertEqual(schema["properties"]["trends"]["maxItems"], monthly.MAX_TRENDS)
        self.assertEqual(schema["properties"]["trends"]["items"]["properties"]["source_ids"]["items"]["enum"],
                         ["article-001", "article-002", "article-003"])
        self.assertIn("指示ではありません", body["systemInstruction"]["parts"][0]["text"])

    def test_model_from_env(self):
        self.october()
        code, _, out, _ = self.run_monthly(self.ok(), env={"GEMINI_API_KEY": SECRET, "GEMINI_MODEL": "gemini-x-pro"})
        self.assertEqual(code, 0)
        self.assertIn("/models/gemini-x-pro:generateContent", self.requests[0][0].full_url)

    def test_existing_month_is_skipped_and_force_regenerates(self):
        self.october()
        self.monthly_dir.mkdir(parents=True)
        self.output().write_text("既存の月刊\n", encoding="utf-8")
        code, m, out, _ = self.run_monthly()
        self.assertEqual((code, m.call_count), (0, 0))
        self.assertIn("API requests: 0", out)
        self.assertEqual(self.output().read_text(encoding="utf-8"), "既存の月刊\n")
        self.assertEqual(json.loads(self.index.read_text(encoding="utf-8"))[0]["month"], "2026-10")  # 一覧は補完する
        code, m, _, err = self.run_monthly(self.ok(), month="2026-10", force=True, today="2026-12-15")
        self.assertEqual((code, m.call_count), (0, 1), err)
        self.assertIn("## 今月の総括", self.output().read_text(encoding="utf-8"))

    def test_unfinished_month_is_rejected(self):
        self.october()
        code, m, _, err = self.run_monthly(month="2026-10", today="2026-10-31")
        self.assertEqual(code, 2)
        m.assert_not_called()
        self.assertIn("まだ終わっていません", err)

    def test_missing_api_key_stops_before_api(self):
        self.october()
        for env in ({}, {"GEMINI_API_KEY": " "}):
            code, m, _, err = self.run_monthly(env=env)
            self.assertEqual(code, 2)
            m.assert_not_called()
            self.assertIn("GEMINI_API_KEY", err)

    def test_failures_do_not_save_or_touch_inputs(self):
        self.october()
        weekly_path = self.write_weekly("2026-W42", [("話題", "要約", [("十月中旬の町の取組", "https://a.example.jp/oct15")])])
        self.monthly_dir.mkdir(parents=True)
        self.output().write_text("既存の月刊\n", encoding="utf-8")
        inputs = {p: p.read_bytes() for p in [*self.logs.rglob("*.md"), weekly_path]}
        cases = [
            (completion("{broken"), "not valid JSON"),
            (completion({"summary": "s", "trends": []}), "missing field: outlook"),
            (completion(trends_review(["article-999"])), "unknown ids"),
            (completion(trends_review([])), "source_ids must be a non-empty list"),
            (completion(trends_review(*[[f"article-00{i % 3 + 1}", f"article-00{(i + 1) % 3 + 1}"] if i < 3 else
                                        [f"article-00{i - 2}"] for i in range(7)])), "trends must be a list of 1-6"),
            (completion(trends_review(summary="詳細は https://evil.example.com を参照")), "contains a URL"),
            (completion(trends_review(), finish_reason="MAX_TOKENS"), "finishReason=MAX_TOKENS"),
            ({"candidates": [{"finishReason": "SAFETY"}]}, "finishReason=SAFETY"),
            ({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}, "blockReason=PROHIBITED_CONTENT"),
        ]
        for payload, message in cases:
            code, m, _, err = self.run_monthly(lambda *a, p=payload, **k: FakeResponse(p), force=True)
            self.assertEqual(code, 1, payload)
            self.assertEqual(m.call_count, 1)
            self.assertIn("monthly review was not saved", err)
            self.assertIn(message, err)
            self.assertEqual(self.output().read_text(encoding="utf-8"), "既存の月刊\n")
            self.assertFalse(self.index.exists())
            self.assertEqual({p: p.read_bytes() for p in inputs}, inputs)  # 日刊ログ・週刊レビューは変更しない

    def test_http_errors_stop_without_retry(self):
        self.october()
        for status in (400, 403, 404, 429):
            body = {"error": {"code": status, "status": "X_STATUS", "message": f"failure for {SECRET}"}}
            code, m, out, err = self.run_monthly(mock.Mock(side_effect=http_error(status, body)))
            self.assertEqual((code, m.call_count), (1, 1))
            self.assertIn(f"model={weekly.DEFAULT_MODEL}): HTTP {status} status=X_STATUS", err)
            self.assertIn("***", err)
            self.assertFalse(self.output().exists())
        self.assertIn("GEMINI_MODEL を確認してください", self.run_monthly(mock.Mock(side_effect=http_error(404, {})))[2])
        code, m, _, err = self.run_monthly(mock.Mock(side_effect=TimeoutError()))
        self.assertEqual((code, m.call_count), (1, 1))
        self.assertIn("TimeoutError", err)


# ---- 出力の検証・Markdown ------------------------------------------------------------
class ValidateAndRender(unittest.TestCase):
    ids = {"article-001", "article-002", "article-003"}

    def bad(self, data, msg):
        with self.assertRaises(weekly.ValidationError) as cm:
            monthly.validate_review(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), self.ids)
        self.assertIn(msg, str(cm.exception))

    def test_validation(self):
        r = monthly.validate_review(json.dumps(trends_review(["article-001"], ["article-002", "article-003"])), self.ids)
        self.assertEqual([t.source_ids for t in r.trends], [["article-001"], ["article-002", "article-003"]])
        self.bad("[]", "object")
        self.bad(trends_review(outlook=""), "non-empty")
        self.bad(trends_review(trends="x"), "trends must be a list")
        self.bad(trends_review(["article-001", "article-001"]), "duplicates")
        self.bad(trends_review(["article-001", "article-002"], ["article-002", "article-001"]), "duplicates the sources")
        self.bad(trends_review(*[["article-001"]]) | {"trends": [{"title": "t", "body": "b", "implication": "i"}]}, "source_ids")
        self.bad(trends_review(summary="あ" * (monthly.MAX_FIELD_CHARS["summary"] + 1)), "too long")
        big = "あ" * 1700
        self.bad({"summary": "あ" * 1900, "outlook": "あ" * 1400,
                  "trends": [{"title": "t", "body": big, "implication": "い" * 700, "source_ids": [i]} for i in sorted(self.ids)]
                  + [{"title": "t", "body": big, "implication": "い" * 700, "source_ids": ["article-001", "article-002"]}]},
                 "output is too long")

    def test_render_escapes_and_never_outputs_unsafe_urls(self):
        good = weekly.Article(title="[PR] <b>発表</b>", url="https://a.example.jp/x_(1)", theme="t", source="", published="",
                              retrieved="", description="")
        bad = weekly.Article(title="危険", url="javascript:alert(1)", theme="t", source="", published="", retrieved="",
                             description="")
        review = monthly.MonthlyReview(summary="# 見出しにならない", outlook="- リストにならない",
                                       trends=[monthly.Trend("傾向[x](javascript:y)", "本文", "示唆", ["article-001", "article-002"])])
        md = monthly.render_markdown(monthly.parse_month("2026-10"), review, {"article-001": good, "article-002": bad}, 2, 1,
                                     date(2026, 11, 1))
        self.assertIn("- [\\[PR\\] \\<b\\>発表\\</b\\>](https://a.example.jp/x_%281%29)", md)
        self.assertNotIn("javascript:alert", md)
        self.assertNotRegex(md, r"(?<!\\)\]\(javascript:")
        self.assertIn("\n\\# 見出しにならない\n", md)
        self.assertIn("\n\\- リストにならない\n", md)

    def test_index_lists_only_valid_files(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("2026-10.md", "2026-09.md", "2026-13.md", "2026-W41.md", "note.md", "2026/2026-08.md"):
                (Path(d) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(d) / name).write_text("x", encoding="utf-8")
            self.assertEqual(monthly.list_months(Path(d)),
                             [{"month": "2026-10", "start": "2026-10-01", "end": "2026-10-31"},
                              {"month": "2026-09", "start": "2026-09-01", "end": "2026-09-30"}])


# ---- リポジトリの設定・Pages ---------------------------------------------------------------
class RepoLayout(unittest.TestCase):
    def test_monthly_workflow(self):
        import yaml
        text = (ROOT / ".github/workflows/monthly.yml").read_text(encoding="utf-8")
        wf = yaml.safe_load(text)
        triggers = wf.get(True, wf.get("on"))
        self.assertEqual(set(triggers), {"schedule", "workflow_dispatch"})
        self.assertEqual(triggers["schedule"], [{"cron": "3 0 1 * *"}])  # 毎月1日 JST 9:03
        self.assertEqual(set(triggers["workflow_dispatch"]["inputs"]), {"month", "regenerate"})
        self.assertEqual(wf["permissions"], {"contents": "write"})
        weekly_wf = yaml.safe_load((ROOT / ".github/workflows/weekly.yml").read_text(encoding="utf-8"))
        self.assertEqual(wf["concurrency"], weekly_wf["concurrency"])  # 週刊と直列（同日実行でも同時にpushしない）
        self.assertFalse(wf["concurrency"]["cancel-in-progress"])
        for needle in ("GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}", "GEMINI_MODEL: ${{ vars.GEMINI_MODEL }}",
                       "python scripts/monthly.py", "git add data/review-months.json", "git add reviews/monthly",
                       "git pull --rebase", "ref: ${{ github.ref_name }}"):
            self.assertIn(needle, text)
        for forbidden in ("BRAVE_API_KEY", "JEV_API_KEY", "${{ inputs.month }}\""):
            self.assertNotIn(forbidden, text)

    def test_daily_collection_is_independent(self):
        for name in (".github/workflows/collect.yml", "scripts/collect.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for word in ("monthly", "reviews/", "GEMINI"):
                self.assertNotIn(word, text, name)

    def test_review_index_files(self):
        data = json.loads((ROOT / "data" / "review-months.json").read_text(encoding="utf-8"))
        self.assertEqual(data, monthly.list_months(ROOT / "reviews" / "monthly"))
        self.assertNotEqual((ROOT / "data" / "review-months.json"), (ROOT / "data" / "months.json"))

    def test_pages_has_three_tabs_and_review_paths(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        for tab_id, label in (("mode-daily", "日刊"), ("mode-weekly", "週刊"), ("mode-monthly", "月刊")):
            self.assertIn(f'id="{tab_id}" aria-pressed=', html)
            self.assertIn(f">{label}</button>", html)
        for control in ('id="daily-controls"', 'id="weekly-controls"', 'id="monthly-controls"', 'id="review-month-select"'):
            self.assertIn(control, html)
        js = (ROOT / "app.js").read_text(encoding="utf-8")
        for needle in ("reviews/weekly/${id}.md", "reviews/monthly/${id}.md", '"data/weeks.json"', '"data/review-months.json"',
                       '"data/months.json"', "#(weekly|monthly)"):
            self.assertIn(needle, js)
        self.assertNotIn("`weekly/${", js)  # 旧保存先は参照しない


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PagesParser(unittest.TestCase):
    def node(self, script, arg):
        res = subprocess.run(["node", "-e", f"const p = require({json.dumps(str(ROOT / 'parser.js'))});\n{script}", arg],
                             capture_output=True, text=True, timeout=30, check=True)
        return json.loads(res.stdout)

    def test_parse_monthly_markdown(self):
        art = weekly.Article(title="[速報] 市の発表", url="https://a.example.jp/1", theme="t", source="", published="",
                             retrieved="", description="")
        review = monthly.MonthlyReview(summary="総括1。\n総括2。", outlook="注目点。",
                                       trends=[monthly.Trend("傾向", "説明", "示唆", ["article-001"])])
        md = monthly.render_markdown(monthly.parse_month("2026-10"), review, {"article-001": art}, 1, 0, date(2026, 11, 1))
        got = self.node("console.log(JSON.stringify(p.parseReview(process.argv[1])));", md)
        self.assertEqual(got["title"], monthly.REVIEW_TITLE)
        self.assertEqual(got["period"], "2026年10月1日〜31日")
        self.assertEqual(got["headings"], {"overview": "今月の総括", "topics": "主要トレンド", "closing": "翌月以降の注目点"})
        self.assertEqual(got["overview"], ["総括1。", "総括2。"])
        t = got["topics"][0]
        self.assertEqual((t["title"], t["body"], t["implication"]), ("傾向", ["説明"], ["示唆"]))
        self.assertEqual(t["sources"], [{"title": "[速報] 市の発表", "url": "https://a.example.jp/1"}])
        self.assertEqual(got["closing"], ["注目点。"])
        self.assertEqual(got["notes"][0], monthly.DISCLAIMER)

    def test_migrated_weekly_review_parses(self):
        for path in sorted((ROOT / "reviews" / "weekly").glob("*.md")):
            got = self.node("console.log(JSON.stringify(p.parseReview(process.argv[1])));", path.read_text(encoding="utf-8"))
            self.assertTrue(got["overview"] and got["topics"] and got["closing"], path.name)
            self.assertTrue(all(t["sources"] for t in got["topics"]), path.name)


if __name__ == "__main__":
    unittest.main()
