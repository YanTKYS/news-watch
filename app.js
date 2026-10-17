"use strict";
// 閲覧UI。日刊は data/months.json と logs/YYYY/YYYY-MM.md、週刊は data/weeks.json と reviews/weekly/YYYY-Www.md、
// 月刊は data/review-months.json と reviews/monthly/YYYY-MM.md を相対パスで読み取るだけ。
// 日刊の横断検索は data/search-index.json を初回検索時に1回だけ読み、ブラウザ内で絞り込む。
// 外部由来の文字列は textContent と検証済みURLのhref設定のみで描画する（innerHTML は使わない）。

const MONTH_RE = /^\d{4}-\d{2}$/;
const WEEK_RE = /^\d{4}-W\d{2}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const select = document.getElementById("month-select");
const orderSelect = document.getElementById("order-select");
const dailyControls = document.getElementById("daily-controls");
const modeButtons = {
  daily: document.getElementById("mode-daily"),
  weekly: document.getElementById("mode-weekly"),
  monthly: document.getElementById("mode-monthly"),
};
const searchInput = document.getElementById("search-input");
const searchClear = document.getElementById("search-clear");
const statusEl = document.getElementById("status");
const content = document.getElementById("content");
let loadToken = 0; // 連続切替時に古い応答で上書きしない（日刊・週刊・月刊で共通）
let sortOrder = "desc"; // "desc"=新しい順 / "asc"=古い順（月を切り替えても維持。永続化はしない）
let current = null; // 表示中の月 { ym, days }。並び順変更時の再描画に使う
let mode = "daily"; // "daily" | "weekly" | "monthly"
const SEARCH_PAGE_SIZE = 50; // 検索結果は50件ずつ表示する（「さらに表示」で追加）
const SEARCH_DELAY_MS = 250; // 入力が止まってから検索する（入力途中の再描画を抑える）
let searchQuery = ""; // 検索中のキーワード（空なら通常の日刊表示）
let searchEntries = null; // buildSearchEntries の結果（初回検索時に読み込み、以後は再利用）
let searchLoading = null; // 読み込み中の Promise（同時に複数回取得しない）
let searchTimer = 0;

// 週刊・月刊の設定。一覧（index）は初めてそのタブを開いたときに読む。
const REVIEWS = {
  weekly: {
    index: "data/weeks.json",
    key: "week",
    keyRe: WEEK_RE,
    path: (id) => `reviews/weekly/${id}.md`,
    title: (id) => weekLabel(id),
    option: (e) => `${weekLabel(e.week)}（${rangeLabel(e.start, e.end)}）`,
    name: "週刊レビュー",
    listName: "週の一覧",
    empty: "週刊レビューはまだありません。毎週月曜日に、前週分のレビューを掲載します。",
    select: document.getElementById("week-select"),
    controls: document.getElementById("weekly-controls"),
    entries: null,
  },
  monthly: {
    index: "data/review-months.json",
    key: "month",
    keyRe: MONTH_RE,
    path: (id) => `reviews/monthly/${id}.md`,
    title: (id) => monthLabel(id),
    option: (e) => monthLabel(e.month),
    name: "月刊レビュー",
    listName: "月の一覧",
    empty: "月刊レビューはまだありません。毎月1日に、前月分のレビューを掲載します。",
    select: document.getElementById("review-month-select"),
    controls: document.getElementById("monthly-controls"),
    entries: null,
  },
};

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

  // 「example.jp ・ 2026-10-01」。どちらかが欠けても、あるものだけを表示する。
  const metaText = [item.source, item.published].filter(Boolean).join(" ・ ");
  if (metaText) card.appendChild(el("p", "meta", metaText));

  if (item.description) card.appendChild(el("p", "desc", item.description));

  // リンクと補助情報はカード下部にまとめ、行内のカードで位置を揃える。
  const bottom = el("div", "card-bottom");
  if (url) {
    const link = el("a", "open", "記事を開く →");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    bottom.appendChild(link);
  }
  if (item.queries || item.retrieved || item.theme) {
    const parts = [];
    if (item.retrieved) parts.push(`取得日: ${item.retrieved}`);
    if (item.theme) parts.push(`テーマ: ${item.theme}`); // 検索結果のみ（日刊表示ではテーマは見出し）
    if (item.queries) parts.push(`検索語: ${item.queries}`);
    const foot = el("p", "foot", parts.join(" / "));
    foot.title = parts.join(" / "); // 1行表示で省略された場合に全文を確認できる
    bottom.appendChild(foot);
  }
  if (bottom.childNodes.length) card.appendChild(bottom);
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
    if (latest && day.date === latest) summary.appendChild(el("span", "latest", "最新")); // 日付値で判定（並び順に依存しない）
    section.appendChild(summary);
    for (const theme of day.themes) {
      const block = el("div", "theme");
      const h = el("h4", "theme-title", theme.name);
      h.appendChild(el("span", "count", `${theme.items.length}件`));
      block.appendChild(h);
      const cards = el("div", "theme-cards"); // テーマごとに独立したGrid
      for (const item of theme.items) cards.appendChild(renderCard(item));
      block.appendChild(cards);
      section.appendChild(block);
    }
    frag.appendChild(section);
  }
  content.replaceChildren(frag);
}

async function showMonth(ym) {
  if (mode !== "daily" || searchQuery) return;
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

// 日刊を開く。月の一覧が未取得なら取得し（失敗時は次に開いたとき再試行）、選択中の月を表示する。
async function startDaily() {
  const token = ++loadToken;
  content.replaceChildren();
  setStatus("読み込み中...");
  if (!select.options.length) await loadMonths(token);
  if (token === loadToken && select.value) showMonth(select.value); // months.json は新しい順。先頭=最新月
}

async function loadMonths(token) {
  let months;
  try {
    const data = JSON.parse(await fetchText("data/months.json"));
    if (!Array.isArray(data)) throw new Error("not an array");
    months = data.filter((m) => typeof m === "string" && MONTH_RE.test(m));
  } catch (e) {
    if (token === loadToken) setStatus("月の一覧（data/months.json）を取得できませんでした。", true);
    return;
  }
  if (months.length === 0) {
    if (token === loadToken) setStatus("表示できるログがまだありません。");
    return;
  }
  if (select.options.length) return; // 並行して開いた別の呼び出しが先に一覧を作成済み
  for (const ym of months) {
    const opt = el("option", "", monthLabel(ym));
    opt.value = ym;
    select.appendChild(opt);
  }
  syncDailyControls();
  select.addEventListener("change", () => showMonth(select.value));
  orderSelect.addEventListener("change", () => {
    sortOrder = orderSelect.value === "asc" ? "asc" : "desc";
    if (current) renderMonth(current.ym, current.days, sortOrder); // 再取得せず再描画（開閉は最新日のみ展開に戻る）
  });
}

// 検索中は月の選択・並び順を無効にする（検索は全期間・新しい順）。
function syncDailyControls() {
  const off = Boolean(searchQuery) || !select.options.length;
  select.disabled = off;
  orderSelect.disabled = off;
  searchClear.disabled = !searchInput.value;
}

// 日刊タブの表示: 検索中なら検索結果、そうでなければ選択中の月（取得済みなら再取得せず再描画）。
function showDaily() {
  if (searchQuery) {
    showSearch();
  } else if (current) {
    ++loadToken; // 週刊・月刊・検索の読み込み中の応答で上書きしない
    setStatus("");
    renderMonth(current.ym, current.days, sortOrder);
  } else {
    startDaily();
  }
}

// ---- 日刊の横断検索 -----------------------------------------------------------
function loadSearchEntries() {
  if (!searchLoading) {
    searchLoading = fetchText("data/search-index.json")
      .then((text) => { searchEntries = buildSearchEntries(JSON.parse(text)); })
      .catch((e) => { searchLoading = null; throw e; }); // 失敗時は次の検索で再試行する
  }
  return searchLoading;
}

async function showSearch() {
  const token = ++loadToken;
  const query = searchQuery;
  if (searchEntries === null) {
    content.replaceChildren();
    setStatus("検索データを読み込み中...");
    try {
      await loadSearchEntries();
    } catch (e) {
      if (token === loadToken) setStatus("検索データ（data/search-index.json）を取得できませんでした。", true);
      return;
    }
    if (token !== loadToken) return;
  }
  renderSearch(query, searchArticles(searchEntries, query));
}

function renderSearch(query, results) {
  setStatus("");
  const frag = document.createDocumentFragment();
  const head = el("div", "month-head");
  head.appendChild(el("h2", "", "検索結果"));
  head.appendChild(el("span", "count", `${results.length}件`));
  frag.appendChild(head);
  frag.appendChild(el("p", "search-summary", `「${query}」を全期間の日刊ログから検索（取得日の新しい順）`));
  if (results.length === 0) {
    frag.appendChild(el("p", "search-empty",
      "該当する記事はありません。キーワードを減らすか、別の表記（略称・正式名称など）でお試しください。"));
    content.replaceChildren(frag);
    return;
  }
  const cards = el("div", "theme-cards search-results");
  frag.appendChild(cards);
  const more = el("button", "more");
  more.type = "button";
  let shown = 0;
  const showMore = () => {
    const next = results.slice(shown, shown + SEARCH_PAGE_SIZE);
    for (const item of next) cards.appendChild(renderCard(item));
    shown += next.length;
    more.hidden = shown >= results.length;
    more.textContent = `さらに表示（残り${results.length - shown}件）`;
  };
  more.addEventListener("click", showMore);
  showMore();
  frag.appendChild(more);
  content.replaceChildren(frag);
}

function applySearch(value) {
  clearTimeout(searchTimer);
  const query = value.trim().replace(/\s+/g, " ");
  if (query === searchQuery) return; // 空白の追加など、結果が変わらない入力では再描画しない
  searchQuery = query;
  syncDailyControls();
  if (mode === "daily") showDaily(); // 空欄に戻したら通常の日刊表示へ
}

function initSearch() {
  searchInput.addEventListener("input", (e) => {
    syncDailyControls();
    if (e.isComposing) return; // 日本語入力の変換中は検索しない（確定時に compositionend で検索）
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => applySearch(searchInput.value), SEARCH_DELAY_MS);
  });
  searchInput.addEventListener("compositionend", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => applySearch(searchInput.value), SEARCH_DELAY_MS);
  });
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) applySearch(searchInput.value);
  });
  searchClear.addEventListener("click", () => {
    searchInput.value = "";
    applySearch("");
    searchInput.focus();
  });
}

// ---- 週刊・月刊 -------------------------------------------------------------
function paragraphsOf(parent, texts, className) {
  for (const t of texts) parent.appendChild(el("p", className, t));
}

function renderReview(cfg, entry, review) {
  const article = el("article", "weekly");
  const head = el("header", "weekly-head");
  if (review.title) head.appendChild(el("p", "weekly-kicker", review.title));
  head.appendChild(el("h2", "", cfg.title(entry[cfg.key])));
  head.appendChild(el("p", "weekly-period", `対象期間：${review.period || rangeLabel(entry.start, entry.end)}`));
  article.appendChild(head);

  if (review.overview.length) {
    const sec = el("section", "weekly-section");
    sec.appendChild(el("h3", "", review.headings.overview));
    paragraphsOf(sec, review.overview);
    article.appendChild(sec);
  }

  if (review.topics.length) {
    const sec = el("section", "weekly-section");
    sec.appendChild(el("h3", "", review.headings.topics));
    review.topics.forEach((topic, i) => {
      const block = el("section", "topic");
      block.appendChild(el("h4", "topic-title", `${i + 1}. ${topic.title}`));
      paragraphsOf(block, topic.body);
      if (topic.implication.length) {
        const imp = el("div", "implication");
        imp.appendChild(el("p", "implication-label", "自治体実務への示唆"));
        paragraphsOf(imp, topic.implication);
        block.appendChild(imp);
      }
      const links = topic.sources.map((s) => ({ title: s.title, url: safeUrl(s.url) })).filter((s) => s.url);
      if (links.length) {
        const src = el("div", "sources");
        src.appendChild(el("p", "sources-label", "出典"));
        const ul = el("ul");
        for (const s of links) {
          const a = el("a", "", s.title || s.url);
          a.href = s.url;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          const li = el("li");
          li.appendChild(a);
          ul.appendChild(li);
        }
        src.appendChild(ul);
        block.appendChild(src);
      }
      sec.appendChild(block);
    });
    article.appendChild(sec);
  }

  if (review.closing.length) {
    const sec = el("section", "weekly-section");
    sec.appendChild(el("h3", "", review.headings.closing));
    paragraphsOf(sec, review.closing);
    article.appendChild(sec);
  }

  if (review.notes.length) {
    const foot = el("footer", "weekly-notes");
    paragraphsOf(foot, review.notes);
    article.appendChild(foot);
  }
  content.replaceChildren(article);
}

async function showReview(kind, id) {
  const cfg = REVIEWS[kind];
  const entry = cfg.entries && cfg.entries.find((e) => e[cfg.key] === id);
  if (!entry || mode !== kind) return;
  const token = ++loadToken;
  content.replaceChildren();
  setStatus("読み込み中...");
  setHash(`${kind}/${id}`);
  let text;
  try {
    text = await fetchText(cfg.path(id));
  } catch (e) {
    if (token === loadToken) setStatus(`${cfg.title(id)}の${cfg.name}を取得できませんでした。`, true);
    return;
  }
  if (token !== loadToken) return;
  const review = parseReview(text);
  if (!review.overview.length && !review.topics.length) {
    setStatus(`${cfg.title(id)}の${cfg.name}を表示できませんでした。`, true);
    return;
  }
  setStatus("");
  renderReview(cfg, entry, review);
}

async function startReview(kind, preferred) {
  const cfg = REVIEWS[kind];
  const token = ++loadToken;
  content.replaceChildren();
  if (cfg.entries === null) {
    setStatus("読み込み中...");
    let list;
    try {
      const data = JSON.parse(await fetchText(cfg.index));
      if (!Array.isArray(data)) throw new Error("not an array");
      list = data.filter((e) => e && cfg.keyRe.test(e[cfg.key]) && DATE_RE.test(e.start) && DATE_RE.test(e.end));
    } catch (e) {
      if (token === loadToken) setStatus(`${cfg.listName}（${cfg.index}）を取得できませんでした。`, true);
      return; // 次にタブを開いたときに再試行する
    }
    if (cfg.entries === null) {
      cfg.entries = list;
      for (const e of list) {
        const opt = el("option", "", cfg.option(e));
        opt.value = e[cfg.key];
        cfg.select.appendChild(opt);
      }
      cfg.select.disabled = list.length === 0;
    }
  }
  if (token !== loadToken) return;
  if (cfg.entries.length === 0) {
    setStatus(cfg.empty);
    return;
  }
  if (preferred && cfg.entries.some((e) => e[cfg.key] === preferred)) cfg.select.value = preferred;
  showReview(kind, cfg.select.value || cfg.entries[0][cfg.key]); // 一覧は新しい順。先頭=最新
}

// ---- 日刊／週刊／月刊の切り替え（#weekly/2026-W42・#monthly/2026-10 で直接開ける） -----------
function setHash(value) {
  const url = value ? `#${value}` : location.pathname + location.search;
  if (location.hash !== (value ? `#${value}` : "")) history.replaceState(null, "", url);
}

function setMode(next, preferred) {
  mode = next;
  for (const [name, button] of Object.entries(modeButtons)) {
    button.setAttribute("aria-pressed", String(name === next));
  }
  dailyControls.hidden = next !== "daily";
  for (const [kind, cfg] of Object.entries(REVIEWS)) cfg.controls.hidden = next !== kind;
  if (REVIEWS[next]) {
    setHash(next);
    startReview(next, preferred);
  } else {
    setHash("");
    showDaily();
  }
}

function parseHash(hash) {
  const m = /^#(weekly|monthly)(?:\/([0-9A-Za-z-]+))?$/.exec(hash);
  if (!m) return { mode: "daily", id: null };
  const id = m[2] && REVIEWS[m[1]].keyRe.test(m[2]) ? m[2] : null;
  return { mode: m[1], id };
}

function init() {
  initSearch();
  for (const [name, button] of Object.entries(modeButtons)) {
    button.addEventListener("click", () => { if (mode !== name) setMode(name); });
  }
  for (const [kind, cfg] of Object.entries(REVIEWS)) {
    cfg.select.addEventListener("change", () => showReview(kind, cfg.select.value));
  }
  const target = parseHash(location.hash);
  setMode(target.mode, target.id);
}

init();
