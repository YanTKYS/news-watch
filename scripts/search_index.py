#!/usr/bin/env python3
"""日刊ログ（logs/YYYY/YYYY-MM.md）から、Pagesの横断検索用インデックス data/search-index.json を生成する。

- 日刊ログが正本で、インデックスは派生データ（いつでも再生成できる）。外部API・ライブラリは使わない。
- 1記事1行のJSON配列。取得日（retrieved）の新しい順、同じ日はログの出現順。
- 同じURLは最初に記録された1件だけを登録する。http/https 以外のURLの記事は登録しない。
- 内容が変わらなければ書き込まない（collect.yml で変更があるときだけcommitされる）。
- collect.yml では収集の後の別ステップで実行し、失敗しても日刊ログのcommitは妨げない。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNCATEGORIZED = "（未分類）"
_MONTH_FILE_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])\.md$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FIELD_RE = re.compile(r"^-\s*([A-Za-z]+):\s*(.*)$")


def safe_url(value: str) -> str | None:
    """http/https かつホスト名のあるURLのみ許可する。"""
    value = value.strip()
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if any(c.isspace() or ord(c) < 0x20 for c in value):
        return None
    return value


def parse_log(text: str) -> list[dict]:
    """月次ログを記事のリストに変換する（parser.js と同じ見出し階層: ## 日付 > ### テーマ > #### 記事）。

    取得日は Retrieved（無ければ日付見出し）。タイトル・取得日・有効なURLのない記事は含めない。
    """
    articles: list[dict] = []
    day = ""
    theme = UNCATEGORIZED
    cur: dict | None = None

    def flush() -> None:
        if cur is None:
            return
        url = safe_url(cur["url"])
        retrieved = cur["retrieved"] if _DATE_RE.match(cur["retrieved"]) else day
        if url and cur["title"] and _DATE_RE.match(retrieved):
            articles.append({"title": cur["title"], "url": url, "source": cur["source"], "published": cur["published"],
                             "retrieved": retrieved, "theme": theme, "description": " ".join(cur["desc"])})

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#### "):
            flush()
            cur = {"title": line[5:].strip(), "url": "", "source": "", "published": "", "retrieved": "", "desc": []}
        elif line.startswith("#"):
            flush()
            cur = None
            if line.startswith("### "):
                theme = line[4:].strip() or UNCATEGORIZED
            elif line.startswith("## "):
                day, theme = line[3:].strip(), UNCATEGORIZED
        elif cur is not None:
            m = _FIELD_RE.match(line)
            key = m.group(1).lower() if m else None
            if key in ("url", "source", "published", "retrieved"):
                cur[key] = m.group(2).strip()
            elif key == "queries":
                continue  # 検索対象外
            else:
                cur["desc"].append(line)
    flush()
    return articles


def month_files(logs_dir: Path) -> list[Path]:
    """logs/YYYY/YYYY-MM.md（年ディレクトリとファイル名の年が一致するもの）を古い順に返す。"""
    files = []
    for path in logs_dir.glob("*/*.md"):
        m = _MONTH_FILE_RE.match(path.name)
        if m and path.parent.name == m.group(1):
            files.append(path)
    return sorted(files, key=lambda p: p.name)


def build_index(logs_dir: Path) -> list[dict]:
    seen: set[str] = set()
    articles: list[dict] = []
    for path in month_files(logs_dir):
        for a in parse_log(path.read_text(encoding="utf-8")):
            if a["url"] in seen:
                continue
            seen.add(a["url"])
            articles.append(a)
    articles.sort(key=lambda a: a["retrieved"], reverse=True)  # 安定ソート（同じ日はログの出現順）
    return articles


def render(articles: list[dict]) -> str:
    # 1記事1行にして、日々の差分を読みやすくする。生成日時などは入れない（再生成しても同じ内容になる）。
    rows = [json.dumps(a, ensure_ascii=False, separators=(",", ":")) for a in articles]
    return "[\n" + ",\n".join(rows) + "\n]\n" if rows else "[]\n"


def update(index_path: Path, logs_dir: Path) -> tuple[bool, int]:
    """インデックスを生成し、内容が変わったときだけ書き込む。戻り値は (書き込んだか, 記事数)。"""
    articles = build_index(logs_dir)
    new = render(articles)
    if index_path.exists() and index_path.read_text(encoding="utf-8") == new:
        return False, len(articles)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_suffix(index_path.suffix + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(index_path)  # 途中で失敗しても既存のインデックスは壊さない
    return True, len(articles)


def main(logs_dir: Path = ROOT / "logs", index_path: Path = ROOT / "data" / "search-index.json") -> int:
    try:
        changed, count = update(index_path, logs_dir)
    except (OSError, UnicodeDecodeError) as e:
        print(f"::warning::search index was not updated: {type(e).__name__}")
        return 1
    print(f"Search index: {count} articles ({'updated' if changed else 'unchanged'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
