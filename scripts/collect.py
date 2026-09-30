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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
REQUEST_INTERVAL_SEC = 1.1  # Brave APIのレート制限（無料枠 1 req/sec）対策
TIMEOUT_SEC = 15
MAX_RESPONSE_BYTES = 2_000_000
USER_AGENT = "news-watch/1.0 (+https://github.com/YanTKYS/news-watch)"

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
class Stats:
    configured_queries: int = 0
    executed_queries: int = 0
    api_requests: int = 0
    failed_queries: int = 0
    results_received: int = 0
    new_items: int = 0
    duplicate_items: int = 0


# ---- 設定 ---------------------------------------------------------------
def load_config(path: Path) -> list[Theme]:
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
    return themes


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
def build_request(query: str, api_key: str) -> urllib.request.Request:
    params = urllib.parse.urlencode(
        {
            "q": query,
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


def search(query: str, api_key: str) -> list[dict]:
    """1回だけAPIを呼ぶ。失敗時は例外（本文は含めない）。"""
    with urllib.request.urlopen(build_request(query, api_key), timeout=TIMEOUT_SEC) as resp:
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


# ---- メイン処理 ----------------------------------------------------------
def collect(
    themes: list[Theme],
    api_key: str,
    seen: dict[str, dict],
    today: str,
    sleep=time.sleep,
) -> tuple[list[Item], Stats]:
    stats = Stats(configured_queries=sum(len(t.queries) for t in themes))
    if stats.configured_queries > MAX_QUERIES:  # load_configとは独立した最終防衛線
        raise ConfigError(f"検索語が上限を超えています: {stats.configured_queries} 件 (上限 {MAX_QUERIES} 件)")

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
                results = search(query, api_key)
            except Exception as e:  # 1件の失敗で全体を止めない。本文・ヘッダは出力しない。
                stats.failed_queries += 1
                print(f"::warning::search failed: query='{query}' error={describe_error(e)}")
                continue

            for r in results:
                url = normalize_url(r.get("url", "")) if isinstance(r.get("url"), str) else None
                if not url:
                    continue
                stats.results_received += 1
                if url in seen:
                    stats.duplicate_items += 1
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

    stats.new_items = len(new_items)
    return list(new_items.values()), stats


def print_stats(s: Stats) -> None:
    print(f"configured queries: {s.configured_queries}")
    print(f"executed queries: {s.executed_queries}")
    print(f"API requests: {s.api_requests}")
    print(f"failed queries: {s.failed_queries}")
    print(f"results received: {s.results_received}")
    print(f"new items: {s.new_items}")
    print(f"duplicate items: {s.duplicate_items}")


def run(
    config_path: Path = ROOT / "config" / "queries.yml",
    seen_path: Path = ROOT / "data" / "seen.json",
    logs_dir: Path = ROOT / "logs",
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
        themes = load_config(config_path)
        seen = load_seen(seen_path)
        items, stats = collect(themes, api_key, seen, today, sleep=sleep)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if items:
        write_log(logs_dir, today, items)  # ログ→seenの順（途中失敗時に記事を取りこぼさない）
        for item in items:
            seen[item.url] = {"first_seen": today}
        save_seen(seen_path, seen)

    print_stats(stats)
    if stats.failed_queries == stats.executed_queries:
        print("ERROR: すべての検索が失敗しました。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
