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

if (typeof module !== "undefined" && module.exports) {
  module.exports = { parseLog, safeUrl, monthLabel, dateLabel };
}
