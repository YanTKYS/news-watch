#!/usr/bin/env python3
"""Sarashina APIの疎通診断（週刊レビュー生成時の HTTP 403 の原因切り分け用）。

次の2リクエストを1回ずつ送り、結果をActionsログへ出す（最大2リクエスト・リトライなし）。
  A: 最小リクエスト（model と短い挨拶文の messages のみ）
  B: 週刊と同じ形式のリクエスト（weekly.build_request をそのまま使い、記事だけダミー）
ヘッダー（認証方式・User-Agent等）はA・Bとも weekly.py と同じにし、差分を本文だけにする。

- ファイルは一切書き込まない（weekly/・data/weeks.json・logs/ は変更しない）。
- APIキー・Authorizationヘッダー・リクエスト全文・レスポンス全文は出力しない。
  エラー本文はJSONとして解析し、エラーコード・メッセージ等だけを、マスク・長さ制限をして表示する。
- 調査が終わったら、このファイルと .github/workflows/sarashina-diagnostics.yml を削除してよい
  （通常の週刊生成からは参照していない）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import weekly

MINIMAL_PROMPT = "こんにちは"
DIAG_WEEK = "2026-W41"  # Bの本文に入る対象期間の表記にだけ使う（ログは読まない）
MAX_ERROR_BODY_BYTES = 64_000
MAX_TEXT_CHARS = 300
# 調査に必要な範囲のレスポンスヘッダーのみ表示する（Set-Cookie・WWW-Authenticate 等の認証関連は出さない）。
SAFE_HEADERS = ("content-type", "server", "cf-ray", "x-request-id", "request-id", "x-amzn-requestid",
                "x-amz-cf-id", "x-cache", "via", "retry-after")


@dataclass
class Result:
    name: str
    request_keys: list[str]
    request_bytes: int
    status: int | None = None
    ok: bool = False
    network_error: str = ""
    lines: list[tuple[str, str]] = field(default_factory=list)  # 表示用の (項目, 値)。値はマスク済み
    headers: list[tuple[str, str]] = field(default_factory=list)


# ---- リクエスト -------------------------------------------------------------------
def dummy_articles() -> list[weekly.Article]:
    articles = [
        weekly.Article(title="（診断用）A市が生成AIを活用した問い合わせ対応の試行を開始", url="https://example.com/diag-1",
                       theme="自治体の生成AI活用", source="example.com", published="2026-10-05", retrieved="2026-10-06",
                       description="接続診断用のダミー記事です。実在の記事ではありません。"),
        weekly.Article(title="（診断用）B町が窓口業務のデジタル化に関する計画を公表", url="https://example.com/diag-2",
                       theme="自治体DX", source="example.com", published="2026-10-07", retrieved="2026-10-08",
                       description="接続診断用のダミー記事です。実在の記事ではありません。"),
    ]
    weekly.assign_ids(articles)
    return articles


def build_structured_request(api_key: str) -> urllib.request.Request:
    """週刊生成と同じ形式（JSON Schemaの構造化出力・max_completion_tokens）のリクエスト。"""
    return weekly.build_request(weekly.parse_week(DIAG_WEEK), dummy_articles(), api_key)


def build_minimal_request(api_key: str) -> urllib.request.Request:
    """model と messages だけの最小リクエスト。ヘッダーは週刊のリクエストと同一にする。"""
    template = build_structured_request(api_key)
    body = {"model": weekly.MODEL, "messages": [{"role": "user", "content": MINIMAL_PROMPT}]}
    return urllib.request.Request(template.full_url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                  method="POST", headers=dict(template.header_items()))


# ---- 出力の安全化 -------------------------------------------------------------------
_AUTH_RE = re.compile(r"(?i)\b(authorization)\s*[:=]\s*(?:bearer\s+)?[^\s\"',;]+")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[^\s\"',;]+")


def sanitize(value: object, secrets: tuple[str, ...]) -> str:
    """秘密情報をマスクし、制御文字を除いて1行・最大 MAX_TEXT_CHARS 文字にする。"""
    text = str(value)
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    text = _AUTH_RE.sub(r"\1: ***", text)
    text = _BEARER_RE.sub(r"\1 ***", text)
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return text if len(text) <= MAX_TEXT_CHARS else text[: MAX_TEXT_CHARS - 1] + "…"


def safe_headers(headers: object) -> list[tuple[str, str]]:
    if headers is None or not hasattr(headers, "items"):
        return []
    found = {k.lower(): v for k, v in headers.items() if isinstance(k, str)}
    return [(name, str(found[name])) for name in SAFE_HEADERS if name in found]


def _scalar(value: object) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip() != ""


def error_fields(body: bytes) -> list[tuple[str, str]] | None:
    """エラー本文（JSON）から、エラーコード・種別・メッセージ等だけを取り出す。JSONでなければ None。"""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    out: list[tuple[str, str]] = []
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            for key, label in (("code", "Error code"), ("type", "Error type"), ("message", "Error message"),
                               ("param", "Error param")):
                if _scalar(err.get(key)):
                    out.append((label, err[key]))
        elif _scalar(err):
            out.append(("Error message", err))
        for key, label in (("code", "Error code"), ("type", "Error type"), ("message", "Error message")):
            if _scalar(data.get(key)):
                out.append((label, data[key]))
        detail = data.get("detail")  # FastAPI等: 文字列、または [{loc, msg, type}] のリスト
        if _scalar(detail):
            out.append(("Error detail", detail))
        elif isinstance(detail, list):
            for d in detail[:3]:
                if isinstance(d, dict) and _scalar(d.get("msg")):
                    loc = ".".join(str(x) for x in d.get("loc", []) if _scalar(x)) if isinstance(d.get("loc"), list) else ""
                    out.append(("Error detail", f"{loc}: {d['msg']}" if loc else d["msg"]))
        if not out:
            keys = ", ".join(str(k) for k in list(data)[:8])
            out.append(("Error body", f"JSON without known error fields (keys: {keys})"))
    else:
        out.append(("Error body", f"JSON {type(data).__name__} without known error fields"))
    return out


def success_fields(body: bytes, structured_ids: set[str] | None) -> list[tuple[str, str]]:
    """成功応答の概要（本文は出さない）。Bでは構造化出力が週刊の検証を通るかも確認する。"""
    try:
        data = json.loads(body)
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return [("Response", f"unexpected structure ({type(e).__name__})")]
    reason = choice.get("finish_reason")
    out = [("Finish reason", reason if _scalar(reason) else "-")]
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tokens = [f"{k.removesuffix('_tokens')}={usage[k]}" for k in ("prompt_tokens", "completion_tokens", "total_tokens")
              if isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool)]
    if tokens:
        out.append(("Tokens", ", ".join(tokens)))
    if structured_ids is not None:
        if not isinstance(content, str):
            out.append(("Structured output", "invalid (content is not a string)"))
        else:
            try:
                review = weekly.validate_review(content, structured_ids)
                out.append(("Structured output", f"valid ({len(review.topics)} topics)"))
            except weekly.ValidationError as e:
                out.append(("Structured output", f"invalid ({e})"))
    return out


def describe_network_error(e: Exception) -> str:
    if isinstance(e, urllib.error.URLError):
        reason = e.reason
        return f"URLError ({type(reason).__name__}: {reason})" if not isinstance(reason, str) else f"URLError ({reason})"
    return type(e).__name__


# ---- 実行 ------------------------------------------------------------------------
def run_test(name: str, request: urllib.request.Request, secrets: tuple[str, ...],
             structured_ids: set[str] | None = None) -> Result:
    """1回だけ送る（リトライなし）。例外は外へ出さず、結果として返す（Aが失敗してもBを実行できる）。"""
    body_keys = list(json.loads(request.data))
    r = Result(name=name, request_keys=body_keys, request_bytes=len(request.data))
    try:
        with urllib.request.urlopen(request, timeout=weekly.TIMEOUT_SEC) as resp:
            r.status = getattr(resp, "status", None) or resp.getcode()
            r.headers = safe_headers(getattr(resp, "headers", None))
            body = resp.read(weekly.MAX_RESPONSE_BYTES + 1)
        r.ok = 200 <= r.status < 300
        r.lines = success_fields(body, structured_ids)
    except urllib.error.HTTPError as e:
        r.status = e.code
        r.headers = safe_headers(e.headers)
        try:
            body = e.read(MAX_ERROR_BODY_BYTES) or b""
        except Exception:  # 本文を読めなくても診断は続ける
            body = b""
        fields = error_fields(body)
        if fields is None:
            ctype = dict(r.headers).get("content-type", "unknown content-type")
            fields = [("Error body", f"not JSON ({ctype}, {len(body)} bytes)")]
        r.lines = fields
    except Exception as e:  # タイムアウト・名前解決・接続失敗など
        r.network_error = describe_network_error(e)
    r.lines = [(k, sanitize(v, secrets)) for k, v in r.lines]
    r.headers = [(k, sanitize(v, secrets)) for k, v in r.headers]
    r.network_error = sanitize(r.network_error, secrets) if r.network_error else ""
    return r


def print_result(r: Result) -> None:
    print(r.name)
    print(f"Request body: {r.request_bytes} bytes (fields: {', '.join(r.request_keys)})")
    print(f"HTTP status: {r.status if r.status is not None else '- (no response)'}")
    print(f"Result: {'SUCCESS' if r.ok else 'FAILED'}")
    if r.network_error:
        print(f"Network error: {r.network_error}")
    for k, v in r.lines:
        print(f"{k}: {v}")
    for k, v in r.headers:
        print(f"Response header {k}: {v}")
    print()


def interpret(a: Result, b: Result) -> str:
    if a.ok and b.ok:
        text = "A・Bとも成功。一時的な障害、入力サイズ、実データ固有の内容などを追加調査してください。"
    elif a.ok:
        text = "Aのみ成功。JSON Schema・追加パラメータ・構造化出力の利用条件などが疑わしいです。"
    elif b.ok:
        text = "Bのみ成功。リクエスト形式による差異などを追加調査してください。"
    else:
        text = "A・Bとも失敗。GitHub Actions環境（送信元）、アカウント権限、認証方式、送信元制限などが疑わしいです。"
    return text + "HTTPステータスだけでなく、上記のエラー内容を優先して判断してください。"


def run(env: dict | None = None) -> int:
    env = os.environ if env is None else env
    api_key = (env.get("SARASHINA_API_KEY") or "").strip()
    print("Sarashina API diagnostics")
    print()
    print(f"Environment: {'GitHub Actions' if env.get('GITHUB_ACTIONS') == 'true' else 'Local'}")
    print(f"Endpoint: {weekly.API_ENDPOINT}")
    print(f"Model: {weekly.MODEL}")
    print(f"User-Agent: {weekly.USER_AGENT}")
    print()
    if not api_key:
        print("ERROR: 環境変数 SARASHINA_API_KEY が設定されていません（APIは呼び出していません）。", file=sys.stderr)
        print("Total API requests: 0")
        return 2

    secrets = (api_key,)
    structured = build_structured_request(api_key)
    structured_ids = {a.id for a in dummy_articles()}
    results = []
    for name, req, ids in (("Test A: Minimal request", build_minimal_request(api_key), None),
                           ("Test B: Structured request", structured, structured_ids)):
        r = run_test(name, req, secrets, ids)
        print_result(r)
        results.append(r)
    print(f"Total API requests: {len(results)}")
    print(f"Interpretation: {interpret(*results)}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(run())
