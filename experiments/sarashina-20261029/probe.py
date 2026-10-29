#!/usr/bin/env python3
"""Phase 1 only: offline by default, at most three Sarashina requests, no retries."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NEW_MODEL = "sarashina3.2-mini-thinking"
OLD_MODEL = "sarashina3-mini"
BASE_URL = "https://api.sarashina.ai/v1"
MAX_REQUESTS = 3
MAX_COMPLETION_TOKENS = 1024
TIMEOUT_SEC = 90
MAX_RESPONSE_BYTES = 262144
OUTPUT_ROOT = Path(__file__).resolve().parent / "runs"
PROMPT = "日本語で「接続を確認しました。」と一文だけ返してください。"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_request(kind: str, key: str) -> urllib.request.Request:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if kind == "models":
        return urllib.request.Request(f"{BASE_URL}/models", headers=headers, method="GET")
    if kind not in {"new_chat", "old_chat"}:
        raise ValueError("unknown probe")
    body = {"model": NEW_MODEL if kind == "new_chat" else OLD_MODEL,
            "messages": [{"role": "user", "content": PROMPT}],
            "max_completion_tokens": MAX_COMPLETION_TOKENS, "stream": False}
    if kind == "new_chat":
        body["reasoning_effort"] = "low"
    headers["Content-Type"] = "application/json"
    return urllib.request.Request(f"{BASE_URL}/chat/completions", headers=headers,
                                  data=json.dumps(body, ensure_ascii=False).encode(), method="POST")


def open_once(request):
    # Inherit session proxy / CA trust. Redirects are extra requests, so reject them.
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=TIMEOUT_SEC)


def safe_headers(headers) -> dict:
    # Record known header categories only, never arbitrary server-provided strings.
    content = (headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
    server = (headers.get("Server", "") or "").strip().lower()
    return {"content_type": content if content in {"application/json", "text/html", "application/problem+json"}
            else ("other" if content else "missing"),
            "server": server if server in {"awselb/2.0", "nginx", "cloudflare"}
            else ("other" if server else "missing")}


def complete_chat(data) -> bool:
    if not isinstance(data, dict) or data.get("error"):
        return False
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return False
    choice = choices[0]
    message = choice.get("message")
    return (choice.get("finish_reason") == "stop" and isinstance(message, dict)
            and not message.get("refusal") and not message.get("tool_calls")
            and not message.get("function_call") and isinstance(message.get("content"), str)
            and bool(message["content"].strip()))


class Probe:
    """One dispatch per call, with an independent hard request budget."""

    def __init__(self):
        self.requests = 0

    def attempt(self, kind: str, key: str) -> dict:
        if self.requests >= MAX_REQUESTS:
            raise RuntimeError("request budget exhausted")
        request = build_request(kind, key)
        metric = {"kind": kind, "method": request.method,
                  "requested_model": NEW_MODEL if kind == "new_chat" else OLD_MODEL if kind == "old_chat" else None,
                  "http_status": None, "http_class": "no_response", "content_type": "missing",
                  "server": "missing", "elapsed_seconds": 0, "result": "transport_error",
                  "matches_legacy_header_signature": False}
        self.requests += 1  # Attempted dispatch; not a guarantee of server receipt.
        started = time.monotonic()
        try:
            with open_once(request) as response:
                metric["http_status"] = response.status
                metric.update(safe_headers(response.headers))
                raw = response.read(MAX_RESPONSE_BYTES + 1) if response.status == 200 else b""
            if metric["http_status"] != 200:
                metric["result"] = "http_error"
            elif len(raw) > MAX_RESPONSE_BYTES:
                metric["result"] = "response_too_large"
            else:
                data = json.loads(raw)
                if kind == "models":
                    rows = data.get("data") if isinstance(data, dict) and not data.get("error") else None
                    if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get("id"), str)
                                                              for row in rows):
                        metric["result"] = "invalid_models"
                    else:
                        # Neither model IDs beyond our two targets nor arbitrary payloads are saved.
                        ids = {row["id"] for row in rows}
                        metric.update(result="success", new_model_present=NEW_MODEL in ids,
                                      old_model_present=OLD_MODEL in ids)
                else:
                    metric["result"] = "success" if complete_chat(data) else "invalid_completion"
        except urllib.error.HTTPError as error:
            metric["http_status"] = error.code
            metric.update(safe_headers(error.headers or {}))
            metric["result"] = "http_error"
            error.close()  # Do not read/log/save HTTP error bodies, including HTML 403.
        except (ValueError, UnicodeError):
            metric["result"] = "invalid_json"
        except (OSError, http.client.HTTPException):
            metric["result"] = "transport_error"
        finally:
            metric["elapsed_seconds"] = round(time.monotonic() - started, 3)
        status = metric["http_status"]
        metric["http_class"] = str(status) if status in {200, 401, 403, 429} else "other" if status else "no_response"
        metric["matches_legacy_header_signature"] = (status == 403 and metric["content_type"] == "text/html"
                                                       and metric["server"] == "awselb/2.0")
        return metric


def save_results(out: Path, metrics: dict) -> None:
    # Write only explicitly constructed diagnostics, not prompts, responses or exceptions.
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")
    lines = ["# Sarashina Phase 1 probe", "", f"Status: {metrics['status']}", "",
             f"API requests (attempted): {metrics['api_requests']} / maximum {MAX_REQUESTS}", "",
             f"Environment: {metrics['environment']['os']} / {metrics['environment']['runner']}", "",
             "| Probe | HTTP | Content type | Server | Seconds | Result |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in metrics["attempts"]:
        lines.append(f"| {row['kind']} | {row['http_status']} | {row['content_type']} | {row['server']} | "
                     f"{row['elapsed_seconds']} | {row['result']} |")
    lines += ["", "旧結果（Issue #24引用）: ubuntu-latest、sarashina3-mini、HTML 403、server awselb/2.0。",
              "ヘッダーの一致は原因の証明ではない。認証、アカウント、WAF、送信元制限等は未確定。",
              "GET modelsの成功だけではChat Completionsの成功を意味しない。", "",
              f"New chat success: {metrics['new_chat_success']}", "",
              "Phase 2（W42）はこのスクリプトでは実行しない。GitHub Actionsで新モデルの正常応答を確認後、",
              "別手動実行用の後続PRで入力・構造検証・比較を準備する。接続が失敗した場合はW42を送信しない。", ""]
    (out / "report.md").write_text("\n".join(lines))
    (out / "README.md").write_text(
        "# Diagnostic artifact\n\nmetrics.jsonとreport.mdが本実行の証跡。本文・ヘッダー全文・キーは保存しない。\n"
        "HTTPヘッダーは既知の種類のみ記録し、未知の値はotherとする。外部IPは取得しない。\n"
        "api_requestsは送信試行数。通信失敗時のサーバー受信・処理完了回数は保証しない。\n"
        "runner欄は手動指定ラベルであり、Actionsの実行URLとOSを照合すること。\n")


def run(*, execute=False, include_models=True, run_name=None, runner="local", env=None, output_root=OUTPUT_ROOT):
    env = os.environ if env is None else env
    name = run_name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name) or runner not in {"local", "ubuntu-latest"}:
        print("ERROR: invalid run parameters; API requests: 0", file=sys.stderr)
        return 2
    os_name = platform.system()
    metrics = {"phase": 1, "status": "not_run", "api_requests": 0,
               "environment": {"os": os_name if os_name in {"Linux", "Windows", "Darwin"} else "other",
                               "runner": runner},
               "new_chat_success": False, "attempts": []}
    out = output_root / name
    try:
        out.mkdir(parents=True, exist_ok=False)
        save_results(out, metrics)  # Confirm output access before sending anything.
    except OSError:
        print("ERROR: cannot prepare a new output directory; API requests: 0", file=sys.stderr)
        return 2
    result = 0
    if execute:
        key = (env.get("SARASHINA_API_KEY") or "").strip()
        if not key:
            metrics["status"] = "missing_key"
            result = 2
        else:
            probe = Probe()
            metrics["attempts"].append(probe.attempt("new_chat", key))
            metrics["new_chat_success"] = metrics["attempts"][0]["result"] == "success"
            if metrics["new_chat_success"]:
                metrics["attempts"].append(probe.attempt("old_chat", key))
                if include_models:
                    metrics["attempts"].append(probe.attempt("models", key))
                all_ok = all(row["result"] == "success" for row in metrics["attempts"])
                metrics["status"] = "succeeded" if all_ok else "comparison_failed"
            else:
                # Stop on every new-model failure, especially the known HTML 403.
                metrics["status"] = "new_chat_failed"
            metrics["api_requests"] = probe.requests
            result = 0 if metrics["status"] == "succeeded" else 1
    try:
        save_results(out, metrics)
    except OSError:
        print("ERROR: cannot save diagnostics; do not automatically repeat API requests", file=sys.stderr)
        return 1
    print(f"Phase 1 status: {metrics['status']}; API requests: {metrics['api_requests']}")
    if metrics["status"] == "missing_key":
        print("ERROR: SARASHINA_API_KEY is not set; API requests: 0", file=sys.stderr)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--skip-models", action="store_true", help="omit the optional GET /v1/models")
    parser.add_argument("--run-name")
    parser.add_argument("--runner", choices=("local", "ubuntu-latest"), default="local")
    args = parser.parse_args()
    return run(execute=args.execute, include_models=not args.skip_models, run_name=args.run_name, runner=args.runner)


if __name__ == "__main__":
    raise SystemExit(main())
