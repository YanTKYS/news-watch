#!/usr/bin/env python3
"""自治体DX・自治体の生成AI活用に関する新着Web情報をBrave Search APIで収集し、
月単位のMarkdownログへ追記する。

- APIキーは環境変数 BRAVE_API_KEY からのみ取得する（出力・保存しない）。
- 1検索語につきAPIリクエストは1回（ページング・リトライなし）。
- 検索結果のtitle/url/descriptionのみを利用する（本文取得なし）。
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# ---- API利用量のハードリミット -------------------------------------------
API_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
MAX_QUERIES = 10  # 1回の実行で使える検索語の上限。超過は設定異常として停止する。
RESULT_COUNT = 10  # 1リクエストあたりの取得件数（固定・20以下）
MAX_RESULT_COUNT = 20
assert RESULT_COUNT <= MAX_RESULT_COUNT
FRESHNESS = "pw"  # 直近1週間（毎日実行の新着収集向け）
MAX_API_QUERY_CHARS = 400  # Brave Search APIの検索語長の上限（400文字・50語）
MAX_API_QUERY_WORDS = 50
REQUEST_INTERVAL_SEC = 1.1  # Brave APIのレート制限（無料枠 1 req/sec）対策
TIMEOUT_SEC = 15
MAX_RESPONSE_BYTES = 2_000_000
USER_AGENT = "news-watch/1.0 (+https://github.com/YanTKYS/news-watch)"

# ---- Jev（関連性フィルタ）。追加フィルタであり、失敗時は記事を残す（fail-open） -------
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"  # Jev System One API
JEV_MODEL = "jev-latest"  # TypeSafe直API向けの名前（固定したい場合は "jev-1.13.0"）
JEV_QUESTION_KEY = "relevant"
JEV_INSTRUCTIONS = (
    "この情報は、日本の自治体・地方公共団体におけるDX、デジタル化、"
    "生成AI、AI活用、行政業務改善についての具体的なニュース、事例、"
    "制度、調査、実証、導入、サービス、取組に実質的に関係しているか。"
    "単に「DX」「AI」「自治体」等の単語が偶然含まれるだけの記事は false とする。"
)
MAX_JEV_REQUESTS = 20  # 1回の実行でのJev API呼び出しのハードリミット（超過分は判定せず保存）
JEV_TIMEOUT_SEC = 15
JEV_RELEVANCE_THRESHOLD = 0.30  # noul(Yes確率) がこの値未満の場合のみ非関連として除外（Recall優先）
MAX_JEV_RESPONSE_BYTES = 100_000
JEV_REJECT_TTL_DAYS = 7  # 非関連と判定したURLを再判定しない期間（Braveの freshness=pw に合わせる）

JST = timezone(timedelta(hours=9))

# 記事の同一性に影響しないことが明らかなトラッキング用パラメータのみ除去する。
TRACKING_PARAMS = {
    "fbclid", "gclid", "gbraid", "wbraid", "yclid", "msclkid", "dclid",
    "mc_cid", "mc_eid", "igshid", "_hsenc", "_hsmi", "mkt_tok",
}
TRACKING_PREFIXES = ("utm_",)


class ConfigError(Exception):
    """設定・実行前提の異常（開始時点で失敗させる）。"""


@dataclass
class Theme:
    name: str
    queries: list[str]


@dataclass
class Item:
    url: str
    title: str
    description: str
    theme: str
    queries: list[str]
    published: str | None = None


@dataclass
class Config:
    themes: list[Theme]
    exclude_domains: list[str] = field(default_factory=list)
    exclude_url_patterns: list[str] = field(default_factory=list)


@dataclass
class Stats:
    configured_queries: int = 0
    executed_queries: int = 0
    api_requests: int = 0
    failed_queries: int = 0
    results_received: int = 0
    filtered_items: int = 0
    new_items: int = 0
    duplicate_items: int = 0
    jev_candidates: int = 0
    jev_requests: int = 0
    jev_relevant: int = 0
    jev_irrelevant: int = 0
    jev_fallback: int = 0  # APIキー未設定・失敗・上限到達などで判定せず保存した件数
    jev_cached_rejects: int = 0  # 非関連キャッシュにより、Jevを呼ばず除外したユニークURL数


# ---- 設定 ---------------------------------------------------------------
def load_config(path: Path) -> Config:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"設定ファイルを読み込めません: {path} ({type(e).__name__})") from e

    if not isinstance(raw, dict) or not isinstance(raw.get("themes"), list):
        raise ConfigError("設定ファイルの形式が不正です: 'themes' のリストが必要です")

    themes: list[Theme] = []
    seen_queries: set[str] = set()
    for t in raw["themes"]:
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"].strip():
            raise ConfigError("設定ファイルの形式が不正です: 各themeに文字列の 'name' が必要です")
        queries = t.get("queries")
        if not isinstance(queries, list) or not all(isinstance(q, str) and q.strip() for q in queries):
            raise ConfigError(f"設定ファイルの形式が不正です: theme '{t['name']}' の 'queries' は空でない文字列のリストにしてください")
        cleaned = []
        for q in queries:
            q = " ".join(q.split())
            if q in seen_queries:
                raise ConfigError(f"検索語が重複しています: {q}")
            seen_queries.add(q)
            cleaned.append(q)
        themes.append(Theme(name=" ".join(t["name"].split()), queries=cleaned))

    total = len(seen_queries)
    if total == 0:
        raise ConfigError("検索語が0件です")
    if total > MAX_QUERIES:
        raise ConfigError(f"検索語が上限を超えています: {total} 件 (上限 {MAX_QUERIES} 件)")

    domains = _load_exclude_domains(raw.get("exclude_domains", []))
    patterns = _load_exclude_url_patterns(raw.get("exclude_url_patterns", []))
    for q in seen_queries:  # 除外演算子を付けた後の検索語がAPIの上限を超えないこと
        api_q = build_api_query(q, domains)
        if len(api_q) > MAX_API_QUERY_CHARS or len(api_q.split()) > MAX_API_QUERY_WORDS:
            raise ConfigError(f"除外ドメインが多すぎてAPIの検索語長の上限を超えます: {q}")
    return Config(themes=themes, exclude_domains=domains, exclude_url_patterns=patterns)


_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _load_exclude_domains(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError("設定ファイルの形式が不正です: 'exclude_domains' はリストにしてください")
    domains = []
    for d in value:
        if not isinstance(d, str) or not _DOMAIN_RE.match(d.strip().lower()):
            raise ConfigError(f"exclude_domains の値が不正です（例: example.com。http://やパスは不可）: {d!r}")
        domains.append(d.strip().lower())
    return domains


def _load_exclude_url_patterns(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError("設定ファイルの形式が不正です: 'exclude_url_patterns' はリストにしてください")
    patterns = []
    for p in value:
        if not isinstance(p, str) or not p.strip():
            raise ConfigError(f"exclude_url_patterns の値が不正です（空でない文字列）: {p!r}")
        p = p.strip()
        host, sep, rest = p.partition("/")  # ホスト部分のみ大文字小文字を区別しない
        patterns.append(host.lower() + sep + rest)
    return patterns


# ---- URL・テキスト整形 ---------------------------------------------------
def normalize_url(url: str) -> str | None:
    """安全側の最小限の正規化。http(s)以外は None を返す。"""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    query = urllib.parse.urlencode(
        [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
        ]
    )
    return urllib.parse.urlunsplit((scheme, host, path, query, ""))


_TAG_RE = re.compile(r"<[^>]*>")


def clean_text(text: object) -> str:
    """HTMLタグ・実体参照を除去し1行に整形する（Markdown見出しの誤認防止のため先頭の#も除く）。"""
    if not isinstance(text, str):
        return ""
    text = html.unescape(_TAG_RE.sub("", text))
    return " ".join(text.split()).lstrip("#> ").strip()


def is_excluded(url: str, domains: list[str], patterns: list[str]) -> bool:
    """正規化済みURLが除外ドメイン（サブドメイン含む・境界考慮）または除外URLパターンに該当するか。"""
    host = domain_of(url).lower()
    if any(host == d or host.endswith("." + d) for d in domains):
        return True
    target = url.split("://", 1)[-1]  # スキームを除いた host/path?query に対する部分一致
    return any(p in target for p in patterns)


def domain_of(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or ""


def parse_published(value: object) -> str | None:
    """APIが返すISO形式の日付(page_age)が解釈できる場合のみ YYYY-MM-DD を返す。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


# ---- Brave Search API ----------------------------------------------------
def build_api_query(query: str, exclude_domains: list[str] = ()) -> str:
    """APIへ送る検索語。除外ドメインを NOT site: で付与する（ログ・記録には元の検索語を使う）。"""
    return " ".join([query, *(f"NOT site:{d}" for d in exclude_domains)])


def build_request(query: str, api_key: str, exclude_domains: list[str] = ()) -> urllib.request.Request:
    params = urllib.parse.urlencode(
        {
            "q": build_api_query(query, exclude_domains),
            "count": RESULT_COUNT,
            "country": "JP",
            "search_lang": "ja",
            "freshness": FRESHNESS,
        }
    )
    return urllib.request.Request(
        f"{API_ENDPOINT}?{params}",
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-Subscription-Token": api_key,
        },
    )


def search(query: str, api_key: str, exclude_domains: list[str] = ()) -> list[dict]:
    """1回だけAPIを呼ぶ。失敗時は例外（本文は含めない）。"""
    with urllib.request.urlopen(build_request(query, api_key, exclude_domains), timeout=TIMEOUT_SEC) as resp:
        body = resp.read(MAX_RESPONSE_BYTES)
    data = json.loads(body)
    results = ((data.get("web") or {}).get("results")) if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    return [r for r in results if isinstance(r, dict)][:RESULT_COUNT]


def describe_error(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} (HTTPError)"
    return type(e).__name__


# ---- Jev関連性判定 -------------------------------------------------------
def build_jev_request(item: "Item", api_key: str) -> urllib.request.Request:
    """Braveから取得済みの情報のみを state に渡す（本文取得・APIキーは含めない）。"""
    lines = [f"Title: {item.title}", f"Source: {domain_of(item.url)}"]
    if item.description:
        lines.append(f"Description: {item.description}")
    lines.append(f"Search queries: {' / '.join(item.queries)}")
    body = {
        "model": JEV_MODEL,
        "state": "\n".join(lines),
        "questions": {JEV_QUESTION_KEY: {"type": "noul", "instructions": JEV_INSTRUCTIONS}},
    }
    return urllib.request.Request(
        JEV_ENDPOINT,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {api_key}",
        },
    )


def jev_relevance(item: "Item", api_key: str) -> float:
    """1記事につき1回だけJevへ問い合わせ、noul(Yes確率)を返す。失敗・不正な応答は例外（リトライしない）。"""
    with urllib.request.urlopen(build_jev_request(item, api_key), timeout=JEV_TIMEOUT_SEC) as resp:
        data = json.loads(resp.read(MAX_JEV_RESPONSE_BYTES))
    answer = data["answers"][JEV_QUESTION_KEY]
    value = answer["noul"]
    if answer.get("type") != "noul" or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("unexpected answer")
    if not 0.0 <= value <= 1.0:
        raise ValueError("noul out of range")
    return float(value)


def is_recently_rejected(rejected: dict[str, dict], url: str, today: str) -> bool:
    """非関連キャッシュ（jev-rejected.json）に、TTL以内で登録されているURLか。"""
    entry = rejected.get(url)
    try:
        age = (date.fromisoformat(today) - date.fromisoformat(entry["rejected_at"])).days
    except (TypeError, KeyError, ValueError):
        return False
    return age <= JEV_REJECT_TTL_DAYS


def load_rejected(path: Path) -> dict[str, dict]:
    """キャッシュなので、壊れていても収集は止めず空として扱う（Jev呼び出しが増えるだけ・上限あり）。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        print(f"::warning::jev-rejected cache ignored: error={type(e).__name__}")
        return {}
    return {u: v for u, v in data.items() if isinstance(v, dict)}


def update_rejected(path: Path, rejected: dict[str, dict], new_urls: list[str], today: str) -> bool:
    """新たな非関連URLがある場合のみ書き込む（期限切れの掃除もこのとき行う）。"""
    if not new_urls:
        return False
    for url in new_urls:
        rejected[url] = {"rejected_at": today}
    kept = {u: v for u, v in rejected.items() if is_recently_rejected({u: v}, u, today)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kept, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True


def filter_by_relevance(items: list["Item"], jev_api_key: str, stats: Stats) -> tuple[list["Item"], list[str]]:
    """既存フィルタ・既知URL除外・同一実行内の統合を終えたユニーク候補だけをJevで判定する。

    明確に非関連（noul < 閾値）と正常に判定された記事のみ除外し、それ以外は保存する（fail-open）。
    戻り値は (保存する記事, 非関連と判定したURL)。
    """
    stats.jev_candidates = len(items)
    kept: list[Item] = []
    rejected_urls: list[str] = []
    for item in items:
        if not jev_api_key or stats.jev_requests >= MAX_JEV_REQUESTS:
            kept.append(item)
            stats.jev_fallback += 1
            continue
        stats.jev_requests += 1
        try:
            score = jev_relevance(item, jev_api_key)
        except Exception as e:  # リトライしない。本文・ヘッダ・キーは出力しない。
            kept.append(item)
            stats.jev_fallback += 1
            print(f"::warning::jev failed (kept): error={describe_error(e)}")
            continue
        if score < JEV_RELEVANCE_THRESHOLD:
            stats.jev_irrelevant += 1
            rejected_urls.append(item.url)
        else:
            stats.jev_relevant += 1
            kept.append(item)
    stats.new_items = len(kept)
    return kept, rejected_urls


# ---- 永続化 --------------------------------------------------------------
def load_seen(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"seen.json を読み込めません: {path} ({type(e).__name__})") from e
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise ConfigError(f"seen.json の形式が不正です: {path}")
    return data


def save_seen(path: Path, seen: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def render_entry(item: Item, today: str) -> list[str]:
    lines = [
        f"#### {item.title}",
        "",
        f"- URL: {item.url}",
        f"- Source: {domain_of(item.url)}",
        f"- Queries: {' / '.join(item.queries)}",
    ]
    if item.published:
        lines.append(f"- Published: {item.published}")
    lines.append(f"- Retrieved: {today}")
    if item.description:
        lines += ["", item.description]
    return lines


def _insert(lines: list[str], pos: int, new: list[str]) -> None:
    """posの直前の空行を除いた位置へ、空行区切りで new を挿入する。"""
    while pos > 0 and lines[pos - 1].strip() == "":
        pos -= 1
    lines[pos:pos] = ["", *new]


def add_entry(lines: list[str], today: str, theme: str, block: list[str]) -> None:
    """月次ログ(行リスト)へ 日付 > テーマ の見出し配下に記事を追記する。"""
    date_h, theme_h = f"## {today}", f"### {theme}"
    if date_h not in lines:
        _insert(lines, len(lines), [date_h, "", theme_h, "", *block])
        return
    start = lines.index(date_h) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    theme_at = next((i for i in range(start, end) if lines[i] == theme_h), None)
    if theme_at is None:
        _insert(lines, end, [theme_h, "", *block])
        return
    theme_end = next((i for i in range(theme_at + 1, end) if lines[i].startswith("### ")), end)
    _insert(lines, theme_end, block)


def write_log(logs_dir: Path, today: str, items: list[Item]) -> Path:
    year, month = today[:4], today[5:7]
    path = logs_dir / year / f"{year}-{month}.md"
    if path.exists():
        lines = path.read_text(encoding="utf-8").split("\n")
        while lines and lines[-1] == "":
            lines.pop()
    else:
        lines = [f"# {year}年{int(month)}月"]
    for item in items:
        add_entry(lines, today, item.theme, render_entry(item, today))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


_MONTH_FILE_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])\.md$")


def list_months(logs_dir: Path) -> list[str]:
    """logs/YYYY/YYYY-MM.md を走査し、閲覧UI用の月一覧（YYYY-MM・新しい順・重複なし）を返す。"""
    months = set()
    for path in logs_dir.glob("*/*.md"):
        m = _MONTH_FILE_RE.match(path.name)
        if m and path.parent.name == m.group(1):  # 年ディレクトリとファイル名の年が一致するもののみ
            months.add(f"{m.group(1)}-{m.group(2)}")
    return sorted(months, reverse=True)


def update_months(months_path: Path, logs_dir: Path) -> bool:
    """months.json を生成・更新する。内容が変わらなければ書き込まない。変更したら True。"""
    new = json.dumps(list_months(logs_dir), ensure_ascii=False, indent=2) + "\n"
    if months_path.exists() and months_path.read_text(encoding="utf-8") == new:
        return False
    months_path.parent.mkdir(parents=True, exist_ok=True)
    months_path.write_text(new, encoding="utf-8")
    return True


# ---- メイン処理 ----------------------------------------------------------
def collect(
    config: Config,
    api_key: str,
    seen: dict[str, dict],
    today: str,
    sleep=time.sleep,
    rejected: dict[str, dict] | None = None,
) -> tuple[list[Item], Stats]:
    themes = config.themes
    stats = Stats(configured_queries=sum(len(t.queries) for t in themes))
    if stats.configured_queries > MAX_QUERIES:  # load_configとは独立した最終防衛線
        raise ConfigError(f"検索語が上限を超えています: {stats.configured_queries} 件 (上限 {MAX_QUERIES} 件)")

    rejected = rejected or {}  # Jev有効時のみ渡される非関連キャッシュ
    cached_rejects: set[str] = set()
    new_items: dict[str, Item] = {}
    first_request = True
    for theme in themes:
        for query in theme.queries:
            if not first_request:
                sleep(REQUEST_INTERVAL_SEC)
            first_request = False
            stats.executed_queries += 1
            stats.api_requests += 1
            try:
                results = search(query, api_key, config.exclude_domains)
            except Exception as e:  # 1件の失敗で全体を止めない。本文・ヘッダは出力しない。
                stats.failed_queries += 1
                print(f"::warning::search failed: query='{query}' error={describe_error(e)}")
                continue

            for r in results:
                url = normalize_url(r.get("url", "")) if isinstance(r.get("url"), str) else None
                if not url:
                    continue
                stats.results_received += 1
                if is_excluded(url, config.exclude_domains, config.exclude_url_patterns):
                    stats.filtered_items += 1  # 後段フィルタ。ログにもseen.jsonにも残さない
                    continue
                if url in seen:
                    stats.duplicate_items += 1
                elif is_recently_rejected(rejected, url, today):
                    cached_rejects.add(url)  # 直近にJevが非関連と判定済み。再判定せず除外
                elif url in new_items:
                    stats.duplicate_items += 1
                    if query not in new_items[url].queries:
                        new_items[url].queries.append(query)
                else:
                    new_items[url] = Item(
                        url=url,
                        title=clean_text(r.get("title")) or url,
                        description=clean_text(r.get("description")),
                        theme=theme.name,
                        queries=[query],
                        published=parse_published(r.get("page_age")),
                    )

    stats.jev_cached_rejects = len(cached_rejects)
    stats.new_items = len(new_items)
    return list(new_items.values()), stats


def print_stats(s: Stats) -> None:
    print(f"configured queries: {s.configured_queries}")
    print(f"executed queries: {s.executed_queries}")
    print(f"API requests: {s.api_requests}")
    print(f"failed queries: {s.failed_queries}")
    print(f"results received: {s.results_received}")
    print(f"filtered items: {s.filtered_items}")
    print(f"duplicate items: {s.duplicate_items}")
    print(f"Jev candidates: {s.jev_candidates}")
    print(f"Jev requests: {s.jev_requests}")
    print(f"Jev relevant: {s.jev_relevant}")
    print(f"Jev irrelevant: {s.jev_irrelevant}")
    print(f"Jev cached rejects (not re-judged): {s.jev_cached_rejects}")
    print(f"Jev skipped/fallback: {s.jev_fallback}")
    print(f"new items: {s.new_items}")


def run(
    config_path: Path = ROOT / "config" / "queries.yml",
    seen_path: Path = ROOT / "data" / "seen.json",
    logs_dir: Path = ROOT / "logs",
    months_path: Path = ROOT / "data" / "months.json",
    rejected_path: Path = ROOT / "data" / "jev-rejected.json",
    today: str | None = None,
    env: dict | None = None,
    sleep=time.sleep,
) -> int:
    env = os.environ if env is None else env
    today = today or datetime.now(JST).date().isoformat()

    api_key = (env.get("BRAVE_API_KEY") or "").strip()
    if not api_key:
        print("ERROR: 環境変数 BRAVE_API_KEY が設定されていません。"
              "GitHub の Settings → Secrets and variables → Actions で登録してください。", file=sys.stderr)
        return 2
    try:
        config = load_config(config_path)
        seen = load_seen(seen_path)
        jev_api_key = (env.get("JEV_API_KEY") or "").strip()
        # Jev無効時は非関連キャッシュも使わない（従来どおり全件保存）
        rejected = load_rejected(rejected_path) if jev_api_key else {}
        items, stats = collect(config, api_key, seen, today, sleep=sleep, rejected=rejected)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if not jev_api_key:  # Jevは追加フィルタ。未設定でも収集は継続する
        print("::warning::JEV_API_KEY is not configured; relevance filtering is skipped.")
    items, new_rejects = filter_by_relevance(items, jev_api_key, stats)
    update_rejected(rejected_path, rejected, new_rejects, today)

    if items:
        write_log(logs_dir, today, items)  # ログ→seenの順（途中失敗時に記事を取りこぼさない）
        for item in items:
            seen[item.url] = {"first_seen": today}
        save_seen(seen_path, seen)
        update_months(months_path, logs_dir)  # 閲覧UI用。収集の成否には影響させない軽量処理

    print_stats(stats)
    if stats.failed_queries == stats.executed_queries:
        print("ERROR: すべての検索が失敗しました。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
