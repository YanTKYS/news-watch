#!/usr/bin/env python3
"""Issue #19: W42-only PLaMo PoC. Offline by default; --execute sends once."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import weekly

WEEK = "2026-W42"
MODEL = "plamo-3.1-prime"
ENDPOINT = "https://api.platform.preferredai.jp/v1/chat/completions"
OUTPUT_ROOT = weekly.ROOT / "experiments" / "plamo-w42" / "runs"
TIMEOUT_SEC = 180
EXPECTED_ARTICLES = 15
EXPECTED_INPUT_SHA256 = "cc6cf4395784aa96ee4be0b3882f5ce0eb66a72aeac066024dcfdc1f7855299f"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect would be a second request and could forward authorization.
        return None


def prepare(logs_dir: Path) -> tuple[weekly.Week, list[weekly.Article], dict]:
    week = weekly.parse_week(WEEK)
    articles, invalid = weekly.load_week_articles(logs_dir, week)
    selected = weekly.select_articles(articles)
    if invalid or len(articles) != EXPECTED_ARTICLES or len(selected) != EXPECTED_ARTICLES:
        raise weekly.WeeklyError("W42 must contain exactly 15 valid articles")
    weekly.assign_ids(selected)
    user_prompt = weekly.build_user_prompt(week, selected)
    schema = weekly.response_schema([a.id for a in selected])
    manifest = {
        "week": WEEK,
        "articles": [{"id": a.id, "url": a.url} for a in selected],
        "input_sha256": digest([weekly.article_payload(a) for a in selected]),
        "system_prompt_sha256": digest(weekly.SYSTEM_PROMPT),
        "user_prompt_sha256": digest(user_prompt),
        "schema_sha256": digest(schema),
        "schema_adjustments": [],
    }
    if manifest["input_sha256"] != EXPECTED_INPUT_SHA256:
        raise weekly.WeeklyError("W42 input changed; review the comparison baseline before sending")
    return week, selected, manifest


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build_request(week: weekly.Week, articles: list[weekly.Article], key: str) -> urllib.request.Request:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": weekly.SYSTEM_PROMPT},
            {"role": "user", "content": weekly.build_user_prompt(week, articles)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "weekly_review", "strict": True,
                            "schema": weekly.response_schema([a.id for a in articles])},
        },
        "max_tokens": weekly.MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    return urllib.request.Request(
        ENDPOINT, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": weekly.USER_AGENT},
    )


def open_once(request):
    # Uses the session proxy and default TLS verification. No retry or tools.
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=TIMEOUT_SEC)


def extract_text(data: dict) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise weekly.ValidationError("expected one choice")
    choice = choices[0]
    message = choice.get("message")
    if choice.get("finish_reason") != "stop" or not isinstance(message, dict):
        raise weekly.ValidationError("incomplete output")
    if message.get("refusal") or message.get("tool_calls") or message.get("function_call"):
        raise weekly.ValidationError("refusal or tool call")
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise weekly.ValidationError("missing text")
    return text


def validate_output(text: str, ids: set[str]) -> weekly.Review:
    # weekly.validate_review checks values, lengths, URLs and source IDs.
    # Check exact fields locally too: the production validator tolerates extras.
    try:
        data = json.loads(text)
    except ValueError as e:
        raise weekly.ValidationError("invalid JSON") from e
    if not isinstance(data, dict) or set(data) != {"overview", "topics", "closing"}:
        raise weekly.ValidationError("unexpected root fields")
    if not isinstance(data["topics"], list):
        raise weekly.ValidationError("invalid topics")
    fields = {"title", "summary", "implication", "source_ids"}
    if any(not isinstance(t, dict) or set(t) != fields for t in data["topics"]):
        raise weekly.ValidationError("unexpected topic fields")
    return weekly.validate_review(text, ids)


def usage_tokens(data: dict) -> dict:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {k: v for k in ("prompt_tokens", "completion_tokens", "total_tokens")
            if type(v := usage.get(k)) is int and v >= 0}


def write_json(path: Path, value) -> None:
    weekly.write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def run(*, execute=False, spec_confirmed=False, logs_dir=weekly.ROOT / "logs", output_root=OUTPUT_ROOT,
        env=None, run_name=None) -> int:
    env = os.environ if env is None else env
    key = (env.get("PLAMO_API_KEY") or "").strip()
    if execute and not key:
        print("ERROR: PLAMO_API_KEY is not set; API requests: 0", file=sys.stderr)
        return 2
    if execute and not spec_confirmed:
        print("ERROR: verify the official API specification before using --spec-confirmed; API requests: 0",
              file=sys.stderr)
        return 2
    name = run_name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
        print("ERROR: invalid run name", file=sys.stderr)
        return 2
    try:
        week, articles, manifest = prepare(logs_dir)
        # Fail before sending if the output is unwritable or a run already exists.
        out = output_root / name
        out.mkdir(parents=True, exist_ok=False)
        write_json(out / "input-manifest.json", manifest)
        metrics = {"week": WEEK, "model": MODEL, "api_requests": 0, "http_status": None,
                   "elapsed_seconds": 0, "status": "not_run", "validation": "not_run",
                   "stage": "not_run", "usage": {}, "spec_confirmed": bool(execute and spec_confirmed)}
        write_json(out / "metrics.json", metrics)
    except weekly.WeeklyError:
        print("ERROR: W42 input is missing, invalid or differs from the comparison baseline; API requests: 0",
              file=sys.stderr)
        return 2
    except OSError:
        # Do not echo paths, arbitrary exception messages, headers or credentials.
        print("ERROR: cannot prepare a new output directory; API requests: 0",
              file=sys.stderr)
        return 2

    result = 0
    if execute:
        request = build_request(week, articles, key)
        started = time.monotonic()
        metrics["api_requests"] = 1  # Attempted dispatch, even if the connection fails.
        metrics["status"] = "failed"
        metrics["stage"] = "request"
        try:
            with open_once(request) as response:
                metrics["http_status"] = response.status
                if response.status != 200:
                    raise weekly.ValidationError("unexpected HTTP status")
                raw = response.read(weekly.MAX_RESPONSE_BYTES + 1)
            if len(raw) > weekly.MAX_RESPONSE_BYTES:
                raise weekly.ValidationError("oversized response")
            metrics["stage"] = "response_json"
            data = json.loads(raw)
            metrics["stage"] = "response_envelope"
            if not isinstance(data, dict) or data.get("error"):
                raise weekly.ValidationError("invalid envelope")
            if data.get("model") != MODEL:
                raise weekly.ValidationError("unexpected model")
            metrics["usage"] = usage_tokens(data)
            metrics["stage"] = "content"
            text = extract_text(data)
            if key in text:
                raise weekly.ValidationError("credential in output")
            metrics["stage"] = "review_validation"
            review = validate_output(text, {a.id for a in articles})
            metrics["validation"] = "passed"
            candidate = weekly.render_markdown(week, review, {a.id: a for a in articles},
                                               len(articles), datetime.now(weekly.JST).date(), MODEL)
            metrics["stage"] = "save_candidate"
            weekly.write_atomic(out / "candidate.md", candidate)
            metrics["status"] = "succeeded"
            metrics["stage"] = "completed"
        except urllib.error.HTTPError as e:
            metrics["http_status"] = e.code
            metrics["error"] = "http_error"
            e.close()
            result = 1
        except (ValueError, weekly.ValidationError):
            metrics["validation"] = "failed"
            metrics["error"] = "invalid_output"
            result = 1
        except (OSError, http.client.HTTPException):
            metrics["error"] = "transport_or_io_error"
            result = 1
        finally:
            metrics["elapsed_seconds"] = round(time.monotonic() - started, 3)

    # Always report only safe fixed fields; no raw responses or exception strings.
    try:
        write_json(out / "metrics.json", metrics)
        summary = (f"# PLaMo W42 run\n\nModel: {MODEL}\n\n"
                   f"Status: {metrics['status']} / validation: {metrics['validation']}\n\n"
                   f"API requests (attempted): {metrics['api_requests']} / HTTP: {metrics['http_status']} / "
                   f"elapsed: {metrics['elapsed_seconds']}s\n\n"
                   f"Usage (provider fields): {json.dumps(metrics['usage'])}\n\n"
                   "比較判断: 検証未完了。構造検証の成功だけでは記事品質・採用可否を判断しない。\n"
                   "Gemini W42: 15記事、1回、HTTP 200、63秒、prompt 3444 / output 1133 / total 4577。\n"
                   "モデル間のトークン数は単純比較しない。品質と費用は別途評価する。\n")
        weekly.write_atomic(out / "report.md", summary)
    except OSError:
        print("ERROR: could not save run results; do not automatically repeat the API request", file=sys.stderr)
        return 1
    print(f"Model: {MODEL}; API requests: {metrics['api_requests']}; status: {metrics['status']}; "
          f"validation: {metrics['validation']}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="send exactly one PLaMo request (no retries)")
    parser.add_argument("--spec-confirmed", action="store_true", help="confirm official specification was checked")
    parser.add_argument("--run-name", help="new run directory name; existing runs cannot be overwritten")
    args = parser.parse_args()
    return run(execute=args.execute, spec_confirmed=args.spec_confirmed, run_name=args.run_name)


if __name__ == "__main__":
    raise SystemExit(main())
