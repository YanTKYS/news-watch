"use strict";
// logs/YYYY/YYYY-MM.md のMarkdownを、表示用のデータ構造へ変換する（DOM操作なし）。
//
// 戻り値: [{ date, themes: [{ name, items: [{ title, url, source, queries,
//           published, retrieved, description }] }] }]   ※ファイル中の出現順
// 見出し階層: "## 日付" > "### テーマ" > "#### 記事タイトル"。
// 項目の欠落（Published・説明文など）や未知の行があっても例外にせず、可能な範囲で読み取る。

const FIELD_KEYS = {
  url: "url",
  source: "source",
  queries: "queries",
  published: "published",
  retrieved: "retrieved",
};
const FIELD_RE = /^-\s*([A-Za-z]+):\s*(.*)$/;
const UNCATEGORIZED = "（未分類）";

function parseLog(markdown) {
  const days = [];
  let day = null;
  let theme = null;
  let item = null;

  const ensureTheme = () => {
    if (!day) {
      day = { date: "", themes: [] };
      days.push(day);
    }
    if (!theme) {
      theme = { name: UNCATEGORIZED, items: [] };
      day.themes.push(theme);
    }
  };

  for (const raw of String(markdown || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (line === "") continue;

    if (line.startsWith("#### ")) {
      ensureTheme();
      item = { title: line.slice(5).trim(), url: "", source: "", queries: "", published: "", retrieved: "", description: "" };
      theme.items.push(item);
    } else if (line.startsWith("### ")) {
      ensureTheme();
      theme = { name: line.slice(4).trim() || UNCATEGORIZED, items: [] };
      day.themes.push(theme);
      item = null;
    } else if (line.startsWith("## ")) {
      day = { date: line.slice(3).trim(), themes: [] };
      days.push(day);
      theme = null;
      item = null;
    } else if (line.startsWith("# ")) {
      continue; // 月の見出し
    } else if (item) {
      const m = FIELD_RE.exec(line);
      const key = m && FIELD_KEYS[m[1].toLowerCase()];
      if (key) {
        item[key] = m[2].trim();
      } else {
        item.description = item.description ? `${item.description} ${line}` : line;
      }
    }
  }

  // 記事のないテーマ・日付は捨てる
  for (const d of days) d.themes = d.themes.filter((t) => t.items.length > 0);
  return days.filter((d) => d.themes.length > 0);
}

// http/https のみ許可する。不正・欠落なら null。
function safeUrl(value) {
  try {
    const u = new URL(String(value || "").trim());
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch (e) {
    return null;
  }
}

// "2026-10" -> "2026年10月"（先頭ゼロなし）。形式が不正なら null。
function monthLabel(ym) {
  const m = /^(\d{4})-(\d{2})$/.exec(ym);
  return m ? `${m[1]}年${Number(m[2])}月` : null;
}

// "2026-10-01" -> "2026年10月1日"。解釈できなければ元の文字列。
function dateLabel(ymd) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(ymd);
  return m ? `${m[1]}年${Number(m[2])}月${Number(m[3])}日` : ymd || "日付不明";
}

// 週刊・月刊レビューのMarkdown（scripts/weekly.py・monthly.py が出力する形式）を表示用のデータ構造へ変換する。
//
// 戻り値: { title, period, headings: { overview, topics, closing }, overview: [段落],
//           topics: [{ title, body: [段落], implication: [段落], sources: [{ title, url }] }],
//           closing: [段落], notes: [段落] }
// 見出し: "# 誌名" > "## 3つの節" > "### N. 見出し"。"---" 以降は注記。
// 節は 週刊「今週の概況／注目トピック／今週のまとめ」、月刊「今月の総括／主要トレンド／翌月以降の注目点」。
const IMPLICATION_LABEL = "自治体実務への示唆：";
const SOURCE_RE = /^-\s*\[((?:\\.|[^\]\\])*)\]\((\S+)\)$/;

// Markdownのバックスラッシュエスケープ（\[ \* など）を元の文字に戻す。
function unescapeMd(text) {
  return String(text).replace(/\\([!-\/:-@[-`{-~])/g, "$1");
}

const SECTION_KEYS = [
  ["overview", ["概況", "総括"]],
  ["topics", ["トピック", "トレンド"]],
  ["closing", ["まとめ", "注目点"]],
];

function parseReview(markdown) {
  const r = {
    title: "", period: "", headings: { overview: "", topics: "", closing: "" },
    overview: [], topics: [], closing: [], notes: [],
  };
  let section = null; // "overview" | "topics" | "closing" | "notes" | null
  let topic = null;
  let part = "body"; // トピック内の位置: "body" | "implication" | "sources"

  for (const raw of String(markdown || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (line === "") continue;

    if (line === "---") {
      section = "notes";
      topic = null;
    } else if (section === "notes") {
      r.notes.push(unescapeMd(line));
    } else if (line.startsWith("# ")) {
      r.title = unescapeMd(line.slice(2).trim());
    } else if (line.startsWith("## ")) {
      const name = line.slice(3).trim();
      const found = SECTION_KEYS.find(([, words]) => words.some((w) => name.includes(w)));
      section = found ? found[0] : null;
      if (section) r.headings[section] = name;
      topic = null;
    } else if (line.startsWith("### ")) {
      if (section !== "topics") continue;
      topic = { title: unescapeMd(line.slice(4).replace(/^\d+\.\s*/, "")), body: [], implication: [], sources: [] };
      r.topics.push(topic);
      part = "body";
    } else if (section === null) {
      if (line.startsWith("対象期間：")) r.period = line.slice(5).trim();
    } else if (section === "topics") {
      if (!topic) continue;
      if (line === "出典：") {
        part = "sources";
      } else if (part === "sources") {
        const m = SOURCE_RE.exec(line);
        if (m) topic.sources.push({ title: unescapeMd(m[1]), url: m[2] });
      } else if (line.startsWith(IMPLICATION_LABEL)) {
        part = "implication";
        topic.implication.push(unescapeMd(line.slice(IMPLICATION_LABEL.length)));
      } else {
        topic[part].push(unescapeMd(line));
      }
    } else {
      r[section].push(unescapeMd(line));
    }
  }
  return r;
}

// "2026-W42" -> "2026年第42週"。形式が不正なら null。
function weekLabel(week) {
  const m = /^(\d{4})-W(\d{2})$/.exec(week);
  return m ? `${m[1]}年第${Number(m[2])}週` : null;
}

// ("2026-10-12", "2026-10-18") -> "10月12日〜18日"（年・月をまたぐ場合は省略しない）
function rangeLabel(start, end) {
  const s = /^(\d{4})-(\d{2})-(\d{2})$/.exec(start);
  const e = /^(\d{4})-(\d{2})-(\d{2})$/.exec(end);
  if (!s || !e) return "";
  const head = `${Number(s[2])}月${Number(s[3])}日`;
  if (s[1] !== e[1]) return `${s[1]}年${head}〜${e[1]}年${Number(e[2])}月${Number(e[3])}日`;
  if (s[2] !== e[2]) return `${head}〜${Number(e[2])}月${Number(e[3])}日`;
  return `${head}〜${Number(e[3])}日`;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { parseLog, safeUrl, monthLabel, dateLabel, parseReview, unescapeMd, weekLabel, rangeLabel };
}
