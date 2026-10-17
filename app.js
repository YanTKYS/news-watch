"use strict";
// 閲覧UI。日刊は data/months.json と logs/YYYY/YYYY-MM.md、週刊は data/weeks.json と
// weekly/YYYY/YYYY-Www.md を相対パスで読み取るだけ。
// 外部由来の文字列は textContent と検証済みURLのhref設定のみで描画する（innerHTML は使わない）。

const MONTH_RE = /^\d{4}-\d{2}$/;
const WEEK_RE = /^\d{4}-W\d{2}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const select = document.getElementById("month-select");
const orderSelect = document.getElementById("order-select");
const weekSelect = document.getElementById("week-select");
const dailyControls = document.getElementById("daily-controls");
const weeklyControls = document.getElementById("weekly-controls");
const modeButtons = { daily: document.getElementById("mode-daily"), weekly: document.getElementById("mode-weekly") };
const statusEl = document.getElementById("status");
const content = document.getElementById("content");
let loadToken = 0; // 連続切替時に古い応答で上書きしない（日刊・週刊で共通）
let sortOrder = "desc"; // "desc"=新しい順 / "asc"=古い順（月を切り替えても維持。永続化はしない）
let current = null; // 表示中の月 { ym, days }。並び順変更時の再描画に使う
let mode = "daily"; // "daily" | "weekly"
let weeks = null; // data/weeks.json の内容（初めて週刊を開いたときに読む）

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
  if (item.queries || item.retrieved) {
    const parts = [];
    if (item.retrieved) parts.push(`取得日: ${item.retrieved}`);
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
  if (mode !== "daily") return;
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
  select.disabled = false;
  orderSelect.disabled = false;
  select.addEventListener("change", () => showMonth(select.value));
  orderSelect.addEventListener("change", () => {
    sortOrder = orderSelect.value === "asc" ? "asc" : "desc";
    if (current) renderMonth(current.ym, current.days, sortOrder); // 再取得せず再描画（開閉は最新日のみ展開に戻る）
  });
}

// ---- 週刊 -----------------------------------------------------------------
function paragraphsOf(parent, texts, className) {
  for (const t of texts) parent.appendChild(el("p", className, t));
}

function renderWeekly(entry, review) {
  const article = el("article", "weekly");
  const head = el("header", "weekly-head");
  if (review.title) head.appendChild(el("p", "weekly-kicker", review.title));
  head.appendChild(el("h2", "", weekLabel(entry.week)));
  head.appendChild(el("p", "weekly-period", `対象期間：${review.period || rangeLabel(entry.start, entry.end)}`));
  article.appendChild(head);

  if (review.overview.length) {
    const sec = el("section", "weekly-section");
    sec.appendChild(el("h3", "", "今週の概況"));
    paragraphsOf(sec, review.overview);
    article.appendChild(sec);
  }

  if (review.topics.length) {
    const sec = el("section", "weekly-section");
    sec.appendChild(el("h3", "", "注目トピック"));
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
    sec.appendChild(el("h3", "", "今週のまとめ"));
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

async function showWeek(week) {
  const entry = weeks && weeks.find((w) => w.week === week);
  if (!entry || mode !== "weekly") return;
  const token = ++loadToken;
  content.replaceChildren();
  setStatus("読み込み中...");
  setHash(`weekly/${week}`);
  let text;
  try {
    text = await fetchText(`weekly/${week.slice(0, 4)}/${week}.md`);
  } catch (e) {
    if (token === loadToken) setStatus(`${weekLabel(week)}の週刊レビューを取得できませんでした。`, true);
    return;
  }
  if (token !== loadToken) return;
  const review = parseWeekly(text);
  if (!review.overview.length && !review.topics.length) {
    setStatus(`${weekLabel(week)}の週刊レビューを表示できませんでした。`, true);
    return;
  }
  setStatus("");
  renderWeekly(entry, review);
}

async function startWeekly(preferred) {
  const token = ++loadToken;
  content.replaceChildren();
  if (weeks === null) {
    setStatus("読み込み中...");
    let list;
    try {
      const data = JSON.parse(await fetchText("data/weeks.json"));
      if (!Array.isArray(data)) throw new Error("not an array");
      list = data.filter((w) => w && WEEK_RE.test(w.week) && DATE_RE.test(w.start) && DATE_RE.test(w.end));
    } catch (e) {
      if (token === loadToken) setStatus("週の一覧（data/weeks.json）を取得できませんでした。", true);
      return;
    }
    if (weeks === null) {
      weeks = list;
      for (const w of weeks) {
        const opt = el("option", "", `${weekLabel(w.week)}（${rangeLabel(w.start, w.end)}）`);
        opt.value = w.week;
        weekSelect.appendChild(opt);
      }
      weekSelect.disabled = weeks.length === 0;
    }
  }
  if (token !== loadToken) return;
  if (weeks.length === 0) {
    setStatus("週刊レビューはまだありません。毎週月曜日に、前週分のレビューを掲載します。");
    return;
  }
  if (preferred && weeks.some((w) => w.week === preferred)) weekSelect.value = preferred;
  showWeek(weekSelect.value || weeks[0].week); // weeks.json は新しい順。先頭=最新週
}

// ---- 日刊／週刊の切り替え（URLの #weekly/2026-W42 で週刊を直接開ける） ----------------
function setHash(value) {
  const url = value ? `#${value}` : location.pathname + location.search;
  if (location.hash !== (value ? `#${value}` : "")) history.replaceState(null, "", url);
}

function setMode(next, preferredWeek) {
  mode = next;
  for (const [name, button] of Object.entries(modeButtons)) {
    button.setAttribute("aria-pressed", String(name === next));
  }
  dailyControls.hidden = next !== "daily";
  weeklyControls.hidden = next !== "weekly";
  if (next === "weekly") {
    setHash("weekly");
    startWeekly(preferredWeek);
  } else {
    setHash("");
    if (current) {
      ++loadToken; // 週刊の読み込み中の応答で上書きしない
      setStatus("");
      renderMonth(current.ym, current.days, sortOrder); // 再取得せず再描画
    } else {
      startDaily();
    }
  }
}

function init() {
  modeButtons.daily.addEventListener("click", () => { if (mode !== "daily") setMode("daily"); });
  modeButtons.weekly.addEventListener("click", () => { if (mode !== "weekly") setMode("weekly"); });
  weekSelect.addEventListener("change", () => showWeek(weekSelect.value));
  const m = /^#weekly(?:\/(\d{4}-W\d{2}))?$/.exec(location.hash);
  setMode(m ? "weekly" : "daily", m && m[1]);
}

init();
