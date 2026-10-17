import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import search_index  # noqa: E402
from test_collect import Base as CollectBase, FakeResponse, api_payload, result  # noqa: E402


def month_log(entries, title="# 月"):
    """entries: [(day, theme, title, url, retrieved, description)]。collect.py と同じ形式で書く。"""
    lines = [title]
    last_day = last_theme = None
    for day, theme, t, url, retrieved, desc in entries:
        if day != last_day:
            lines += ["", f"## {day}"]
            last_day, last_theme = day, None
        if theme != last_theme:
            lines += ["", f"### {theme}"]
            last_theme = theme
        lines += ["", f"#### {t}", "", f"- URL: {url}", f"- Source: {url.split('/')[2] if '://' in url else ''}",
                  "- Queries: \"自治体\" \"生成AI\"", "- Published: 2026-01-01"]
        if retrieved:
            lines.append(f"- Retrieved: {retrieved}")
        if desc:
            lines += ["", desc]
    return "\n".join(lines) + "\n"


class IndexBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.logs = d / "logs"
        self.index = d / "data" / "search-index.json"

    def write(self, ym, entries):
        path = self.logs / ym[:4] / f"{ym}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(month_log(entries), encoding="utf-8")
        return path


class BuildIndex(IndexBase):
    def test_multiple_months_and_years_newest_first(self):
        self.write("2025-12", [("2025-12-31", "自治体DX", "年末の記事", "https://a.example.jp/1", "2025-12-31", "説明A")])
        self.write("2026-01", [("2026-01-01", "自治体DX", "元日の記事", "https://a.example.jp/2", "2026-01-01", "説明B"),
                               ("2026-01-01", "自治体の生成AI活用", "同日の2件目", "https://b.example.jp/3", "2026-01-01", ""),
                               ("2026-01-15", "自治体DX", "中旬の記事", "https://a.example.jp/4", "2026-01-15", "複数行の\n説明")])
        self.write("2026-10", [("2026-10-05", "自治体DX", "十月の記事", "https://a.example.jp/5", "2026-10-05", "")])
        arts = search_index.build_index(self.logs)
        self.assertEqual([a["title"] for a in arts], ["十月の記事", "中旬の記事", "元日の記事", "同日の2件目", "年末の記事"])
        self.assertEqual(arts[1], {"title": "中旬の記事", "url": "https://a.example.jp/4", "source": "a.example.jp",
                                   "published": "2026-01-01", "retrieved": "2026-01-15", "theme": "自治体DX",
                                   "description": "複数行の 説明"})
        self.assertEqual(arts[3]["theme"], "自治体の生成AI活用")
        self.assertTrue(all("queries" not in a for a in arts))  # 表示・検索に不要な項目は入れない

    def test_duplicates_invalid_urls_and_incomplete_entries(self):
        self.write("2026-09", [("2026-09-30", "t", "最初の記録", "https://a.example.jp/dup", "2026-09-30", "")])
        self.write("2026-10", [("2026-10-02", "t", "同じURLの再記録", "https://a.example.jp/dup", "2026-10-02", ""),
                               ("2026-10-02", "t", "危険なURL", "javascript:alert(1)", "2026-10-02", ""),
                               ("2026-10-02", "t", "ホストなし", "https://", "2026-10-02", ""),
                               ("2026-10-03", "t", "Retrievedなし（見出しの日付を使う）", "https://a.example.jp/ok", "", "")])
        arts = search_index.build_index(self.logs)
        self.assertEqual([(a["title"], a["retrieved"]) for a in arts],
                         [("Retrievedなし（見出しの日付を使う）", "2026-10-03"), ("最初の記録", "2026-09-30")])

    def test_ignores_files_outside_log_layout(self):
        self.write("2026-10", [("2026-10-01", "t", "正規", "https://a.example.jp/1", "2026-10-01", "")])
        stray = self.logs / "2027" / "2026-11.md"  # 年ディレクトリとファイル名の年が不一致
        stray.parent.mkdir(parents=True)
        stray.write_text(month_log([("2026-11-01", "t", "対象外", "https://a.example.jp/2", "2026-11-01", "")]), encoding="utf-8")
        self.assertEqual([a["title"] for a in search_index.build_index(self.logs)], ["正規"])

    def test_update_is_idempotent_and_writes_only_on_change(self):
        log = self.write("2026-10", [("2026-10-01", "t", "記事", "https://a.example.jp/1", "2026-10-01", "説明")])
        self.assertEqual(search_index.update(self.index, self.logs), (True, 1))
        first = self.index.read_text(encoding="utf-8")
        mtime = self.index.stat().st_mtime_ns
        self.assertEqual(search_index.update(self.index, self.logs), (False, 1))
        self.assertEqual((self.index.read_text(encoding="utf-8"), self.index.stat().st_mtime_ns), (first, mtime))
        self.assertEqual(len(first.splitlines()), 3)  # "[" / 1記事1行 / "]"
        log.write_text(log.read_text(encoding="utf-8").replace("説明", "更新後の説明"), encoding="utf-8")
        self.assertEqual(search_index.update(self.index, self.logs), (True, 1))
        self.assertIn("更新後の説明", self.index.read_text(encoding="utf-8"))

    def test_empty_logs(self):
        self.assertEqual(search_index.update(self.index, self.logs), (True, 0))
        self.assertEqual(json.loads(self.index.read_text(encoding="utf-8")), [])

    def test_unreadable_log_keeps_existing_index(self):
        self.index.parent.mkdir(parents=True)
        self.index.write_text("[]\n", encoding="utf-8")
        bad = self.logs / "2026" / "2026-10.md"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"\xff\xfe broken")
        out = io.StringIO()
        with redirect_stdout(out):
            code = search_index.main(self.logs, self.index)
        self.assertEqual(code, 1)
        self.assertIn("search index was not updated", out.getvalue())
        self.assertEqual(self.index.read_text(encoding="utf-8"), "[]\n")


class CollectIntegration(CollectBase):
    """日刊収集の後に search_index を実行すると、新着記事が検索インデックスに入る（APIはmock）。"""

    def test_index_follows_collected_logs(self):
        def responder(req, timeout=None):
            return FakeResponse(api_payload(result("https://new.example.jp/a", "LGWAN対応の新サービス", "説明文")))

        code, *_ = self.run_collect(responder, today="2026-10-20")
        self.assertEqual(code, 0)
        index = Path(self.tmp.name) / "data" / "search-index.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(search_index.main(self.logs, index), 0)
        arts = json.loads(index.read_text(encoding="utf-8"))
        self.assertEqual([(a["title"], a["url"], a["retrieved"], a["source"]) for a in arts],
                         [("LGWAN対応の新サービス", "https://new.example.jp/a", "2026-10-20", "new.example.jp")])
        before = index.read_text(encoding="utf-8")
        code, *_ = self.run_collect(responder, today="2026-10-21")  # 既知のURLのみ（新着なし）
        with redirect_stdout(io.StringIO()):
            search_index.main(self.logs, index)
        self.assertEqual(index.read_text(encoding="utf-8"), before)  # 変化がなければcommit対象にならない


class RepoLayout(unittest.TestCase):
    def test_collect_workflow_updates_index_after_collection(self):
        import yaml
        wf = yaml.safe_load((ROOT / ".github/workflows/collect.yml").read_text(encoding="utf-8"))
        steps = wf["jobs"]["collect"]["steps"]
        names = [s.get("name") for s in steps]
        i_collect, i_index, i_commit = names.index("Collect"), names.index("Update search index"), names.index(
            "Commit and push if there are new items")
        self.assertLess(i_collect, i_index)
        self.assertLess(i_index, i_commit)
        index_step = steps[i_index]
        self.assertEqual(index_step["run"], "python scripts/search_index.py")
        self.assertTrue(index_step["continue-on-error"])  # 失敗しても日刊ログのcommitは続ける
        self.assertNotIn("env", index_step)  # APIキーを渡さない
        commit = steps[i_commit]["run"]
        self.assertIn("git add logs data/seen.json data/months.json data/jev-rejected.json", commit)
        self.assertIn("git add data/search-index.json", commit)

    def test_collect_script_is_unchanged_by_search(self):
        self.assertNotIn("search_index", (ROOT / "scripts" / "collect.py").read_text(encoding="utf-8"))  # 収集処理は変更しない

    def test_committed_index_is_derived_from_logs(self):
        # ログは増える一方で、インデックスは次回の収集で再生成されるため、「ログから作れる記事の部分集合」であることを確認する
        committed = json.loads((ROOT / "data" / "search-index.json").read_text(encoding="utf-8"))
        rebuilt = {a["url"]: a for a in search_index.build_index(ROOT / "logs")}
        self.assertTrue(committed)
        for a in committed:
            self.assertEqual(rebuilt.get(a["url"]), a)

    def test_pages_search_ui(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        daily = html.split('id="daily-controls"', 1)[1].split('id="weekly-controls"', 1)[0]
        for needle in ('type="search" id="search-input"', 'id="search-clear"', 'for="search-input"'):
            self.assertIn(needle, daily)  # 日刊タブ内にのみ置く
        js = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertNotRegex(js, r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write")  # 外部由来の文字列をHTMLとして扱わない
        self.assertIn('fetchText("data/search-index.json")', js)
        self.assertEqual(js.count('fetchText("data/search-index.json")'), 1)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ClientSearch(unittest.TestCase):
    """parser.js の検索関数（ブラウザ内で実行される処理）を Node.js で確認する。"""

    INDEX = [
        {"title": "大分市、LGWAN環境で生成AIを稼働", "url": "https://a.example.jp/1", "source": "kn.itmedia.co.jp",
         "published": "", "retrieved": "2026-10-06", "theme": "自治体の生成AI活用", "description": "庁内データを活用"},
        {"title": "藤枝市が窓口DXの実証を開始", "url": "https://b.example.jp/2", "source": "www.city.fujieda.shizuoka.jp",
         "published": "", "retrieved": "2026-10-12", "theme": "自治体DX", "description": "書かない窓口の取組"},
        {"title": "Chatbot導入の事例", "url": "https://c.example.jp/3", "source": "prtimes.jp",
         "published": "", "retrieved": "2026-09-30", "theme": "自治体DX", "description": "lgwan経由で提供"},
        {"title": "重複URL", "url": "https://a.example.jp/1", "source": "x", "published": "", "retrieved": "2026-12-01",
         "theme": "", "description": ""},
        {"title": "危険なURL 藤枝市", "url": "javascript:alert(1)", "source": "x", "published": "", "retrieved": "2026-12-01",
         "theme": "", "description": ""},
        {"title": 123, "url": "https://d.example.jp/4", "source": None, "retrieved": "2026-08-01"},
        "not an object",
    ]

    def search(self, *queries):
        script = ("const e = p.buildSearchEntries(JSON.parse(process.argv[1]));"
                  "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(q => p.searchArticles(e, q).map(a => a.url))));")
        res = subprocess.run(["node", "-e", f"const p = require({json.dumps(str(ROOT / 'parser.js'))});\n{script}",
                              json.dumps(self.INDEX, ensure_ascii=False), json.dumps(list(queries), ensure_ascii=False)],
                             capture_output=True, text=True, timeout=30, check=True)
        return json.loads(res.stdout)

    def test_fields_partial_match_and_case(self):
        got = self.search("大分市", "庁内データ", "自治体の生成AI", "fujieda", "LGWAN", "lgwan", "ＬＧＷＡＮ", "Ｃｈａｔｂｏｔ", "窓口")
        self.assertEqual(got, [
            ["https://a.example.jp/1"],  # タイトル
            ["https://a.example.jp/1"],  # 説明文
            ["https://a.example.jp/1"],  # テーマ（部分一致）
            ["https://b.example.jp/2"],  # 情報源
            ["https://a.example.jp/1", "https://c.example.jp/3"],  # 大文字・小文字を区別しない（新しい順）
            ["https://a.example.jp/1", "https://c.example.jp/3"],
            ["https://a.example.jp/1", "https://c.example.jp/3"],  # 全角英字
            ["https://c.example.jp/3"],
            ["https://b.example.jp/2"],  # タイトルと説明文の両方に含まれても1件
        ])

    def test_and_search_order_and_zero(self):
        got = self.search("藤枝市 窓口DX", "藤枝市　LGWAN", "自治体DX", "存在しない語", "", "   ", "[(.*+?^$|\\")
        self.assertEqual(got[0], ["https://b.example.jp/2"])  # AND
        self.assertEqual(got[1], [])  # 全角空白でも区切る
        self.assertEqual(got[2], ["https://b.example.jp/2", "https://c.example.jp/3"])  # 取得日の新しい順
        self.assertEqual(got[3:], [[], [], [], []])  # 0件・空欄・記号でもエラーにならない

    def test_duplicates_and_invalid_urls_are_excluded(self):
        got = self.search("重複", "危険", "自治体", "example.jp")
        self.assertEqual(got[0], [])  # 重複URLは最初の1件のみ
        self.assertEqual(got[1], [])  # 不正なURLは結果に出さない
        self.assertEqual(got[2], ["https://b.example.jp/2", "https://a.example.jp/1", "https://c.example.jp/3"])
        self.assertEqual(got[3], [])  # URLは検索対象外（タイトル・説明文・テーマ・情報源のみ）
        script = ("const e = p.buildSearchEntries(JSON.parse(process.argv[1]));"
                  "console.log(JSON.stringify(e.map(a => [a.url, a.title, a.source])));")
        res = subprocess.run(["node", "-e", f"const p = require({json.dumps(str(ROOT / 'parser.js'))});\n{script}",
                              json.dumps(self.INDEX, ensure_ascii=False)], capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual(json.loads(res.stdout), [  # 文字列でない項目は空文字として扱い、エラーにしない
            ["https://b.example.jp/2", "藤枝市が窓口DXの実証を開始", "www.city.fujieda.shizuoka.jp"],
            ["https://a.example.jp/1", "大分市、LGWAN環境で生成AIを稼働", "kn.itmedia.co.jp"],
            ["https://c.example.jp/3", "Chatbot導入の事例", "prtimes.jp"],
            ["https://d.example.jp/4", "", ""],
        ])


if __name__ == "__main__":
    unittest.main()
