"use strict";
// 閲覧UI。データは data/months.json と logs/YYYY/YYYY-MM.md を相対パスで読み取るだけ。
// 外部由来の文字列は textContent と検証済みURLのhref設定のみで描画する（innerHTML は使わない）。

const MONTH_RE = /^\d{4}-\d{2}$/;
const select = document.getElementById("month-select");
const orderSelect = document.getElementById("order-select");
const statusEl = document.getElementById("status");
const content = document.getElementById("content");
let loadToken = 0; // 連続切替時に古い応答で上書きしない
let sortOrder = "desc"; // "desc"=新しい順 / "asc"=古い順（月を切り替えても維持。永続化はしない）
let current = null; // 表示中の月 { ym, days }。並び順変更時の再描画に使う

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== "") node.textContent = text;
  return node;
}

function setStatus(message, isError) {
  statusEl.textContent = message;
  statusEl.className = isError ? "error" : "";
  statusEl.hidden = !message;
}

async function fetchText(path) {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}

function renderCard(item) {
  const card = el("article", "card");
  const url = safeUrl(item.url);

  const title = el("h5", "card-title");
  if (url) {
    const a = el("a", "", item.title || url);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    title.appendChild(a);
  } else {
    title.textContent = item.title || "（タイトルなし）";
  }
  card.appendChild(title);

  const meta = el("p", "meta");
  if (item.source) meta.appendChild(el("span", "", `Source: ${item.source}`));
  if (item.published) meta.appendChild(el("span", "", `Published: ${item.published}`));
  if (meta.childNodes.length) card.appendChild(meta);

  if (item.description) card.appendChild(el("p", "desc", item.description));

  if (url) {
    const link = el("a", "open", "記事を開く →");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    card.appendChild(link);
  }

  if (item.queries || item.retrieved) {
    const parts = [];
    if (item.retrieved) parts.push(`取得日: ${item.retrieved}`);
    if (item.queries) parts.push(`検索語: ${item.queries}`);
    card.appendChild(el("p", "foot", parts.join(" / ")));
  }
  return card;
}

// 日付順に並べ替えた新しい配列を返す（元の配列は変更しない）。
function sortDays(days, order) {
  const sorted = [...days].sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
  return order === "asc" ? sorted : sorted.reverse();
}

function countItems(themes) {
  return themes.reduce((n, t) => n + t.items.length, 0);
}

function renderMonth(ym, days, order) {
  const total = days.reduce((n, d) => n + countItems(d.themes), 0);
  // 初期展開するのは、並び順に関係なくその月の最新日のみ
  const latest = days.reduce((max, d) => (d.date > max ? d.date : max), "");
  const frag = document.createDocumentFragment();

  const head = el("div", "month-head");
  head.appendChild(el("h2", "", monthLabel(ym)));
  head.appendChild(el("span", "count", `${total}件`));
  frag.appendChild(head);

  for (const day of sortDays(days, order)) {
    const section = el("details", "day");
    section.open = day.date === latest;
    const summary = el("summary", "day-summary");
    summary.appendChild(el("span", "day-title", dateLabel(day.date)));
    summary.appendChild(el("span", "count", `${countItems(day.themes)}件`));
    section.appendChild(summary);
    for (const theme of day.themes) {
      const block = el("div", "theme");
      const h = el("h4", "theme-title", theme.name);
      h.appendChild(el("span", "count", `${theme.items.length}件`));
      block.appendChild(h);
      for (const item of theme.items) block.appendChild(renderCard(item));
      section.appendChild(block);
    }
    frag.appendChild(section);
  }
  content.replaceChildren(frag);
}

async function showMonth(ym) {
  const token = ++loadToken;
  current = null; // 読み込み中・失敗時に旧月を並び順変更で再描画しない
  content.replaceChildren();
  setStatus("読み込み中...");
  let text;
  try {
    text = await fetchText(`logs/${ym.slice(0, 4)}/${ym}.md`);
  } catch (e) {
    if (token === loadToken) setStatus(`${monthLabel(ym)}のログを取得できませんでした。`, true);
    return;
  }
  if (token !== loadToken) return;
  let days;
  try {
    days = parseLog(text);
  } catch (e) {
    setStatus(`${monthLabel(ym)}のログを解析できませんでした。`, true);
    return;
  }
  if (days.length === 0) {
    setStatus(`${monthLabel(ym)}に表示できる記事がありません。`);
    return;
  }
  setStatus("");
  current = { ym, days };
  renderMonth(ym, days, sortOrder);
}

async function init() {
  let months;
  try {
    const data = JSON.parse(await fetchText("data/months.json"));
    if (!Array.isArray(data)) throw new Error("not an array");
    months = data.filter((m) => typeof m === "string" && MONTH_RE.test(m));
  } catch (e) {
    setStatus("月の一覧（data/months.json）を取得できませんでした。", true);
    return;
  }
  if (months.length === 0) {
    setStatus("表示できるログがまだありません。");
    return;
  }
  for (const ym of months) {
    const opt = el("option", "", monthLabel(ym));
    opt.value = ym;
    select.appendChild(opt);
  }
  select.disabled = false;
  orderSelect.disabled = false;
  select.addEventListener("change", () => showMonth(select.value));
  orderSelect.addEventListener("change", () => {
    sortOrder = orderSelect.value === "asc" ? "asc" : "desc";
    if (current) renderMonth(current.ym, current.days, sortOrder); // 再取得せず再描画（開閉は最新日のみ展開に戻る）
  });
  showMonth(months[0]); // months.json は新しい順。先頭=最新月
}

init();
