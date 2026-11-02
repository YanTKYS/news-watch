#!/usr/bin/env python3
"""W43-only comparison: one selected model, offline by default, no retries."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import plamo_weekly_poc as plamo
import weekly

WEEK = "2026-W43"
GEMINI_MODEL = "gemini-3.6-flash"
PLAMO_MODEL = "plamo-3.1-prime"
MODELS = (GEMINI_MODEL, PLAMO_MODEL)
OUTPUT_ROOT = weekly.ROOT / "experiments" / "w43-api-compare" / "runs"
EXPECTED_HASHES = {
    "input_sha256": "6e629ed58a4b246aa4e78f8b1b0f4c6235ebfed1c04dffdc1b0dcef5f9e79de9",
    "system_prompt_sha256": "c30a72eb14f6c5fed7a968ed1b4aec89a231ada871ea6c50e2c7ab34a76d880b",
    "user_prompt_sha256": "9cb3e8014fc7f80d342940e3228f16cd42b393a059e7cfdffdcb2e816686bd6f",
    "schema_sha256": "300748dc2c4aaddc36555eecaa0d0299e4780fbd8e33a983203e10ed0ee6b184",
}


def prepare(logs_dir: Path):
    week = weekly.parse_week(WEEK)
    source, invalid = weekly.load_week_articles(logs_dir, week)
    articles = weekly.select_articles(source)
    if invalid or len(source) != 26 or len(articles) != 26:
        raise weekly.WeeklyError("expected 26 valid W43 articles")
    weekly.assign_ids(articles)
    manifest = {
        "week": WEEK,
        "articles": [{"id": a.id, "url": a.url} for a in articles],
        "input_sha256": plamo.digest([weekly.article_payload(a) for a in articles]),
        "system_prompt_sha256": plamo.digest(weekly.SYSTEM_PROMPT),
        "user_prompt_sha256": plamo.digest(weekly.build_user_prompt(week, articles)),
        "schema_sha256": plamo.digest(weekly.response_schema([a.id for a in articles])),
        "schema_adjustments": [],
    }
    if any(manifest[k] != v for k, v in EXPECTED_HASHES.items()):
        raise weekly.WeeklyError("comparison input, prompt or schema changed")
    if plamo.MODEL != PLAMO_MODEL:
        raise weekly.WeeklyError("shared PoC model changed")
    return week, articles, manifest


def finish_reason(data: dict, model: str) -> str:
    key = "candidates" if model == GEMINI_MODEL else "choices"
    rows = data.get(key)
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    value = row.get("finishReason" if model == GEMINI_MODEL else "finish_reason")
    allowed = {"STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "PROHIBITED_CONTENT", "OTHER",
               "stop", "length", "content_filter", "tool_calls", "function_call"}
    # Unknown provider strings may contain credentials or payload fragments.
    return value if isinstance(value, str) and value in allowed else "missing" if value is None else "other"


def save_results(out: Path, metrics: dict):
    plamo.write_json(out / "metrics.json", metrics)
    weekly.write_atomic(out / "report.md",
        f"# W43 API comparison run\n\nModel: {metrics['model']} / week: {WEEK} / articles: 26\n\n"
        f"Status: {metrics['status']} / stage: {metrics['stage']} / validation: {metrics['validation']}\n\n"
        f"API requests (attempted): {metrics['api_requests']} / HTTP: {metrics['http_status']} / "
        f"elapsed: {metrics['elapsed_seconds']}s / finish: {metrics['finish_reason']}\n\n"
        f"Usage (provider definitions): {json.dumps(metrics['usage'])}\n\n"
        "比較評価は未完了。構造検証の成功は記事品質・採用可否の判断を代替しない。\n"
        "W43のGemini 3.8版は存在しない。W42の過去実測は参考に限定する。\n"
        "候補を本番へ反映せず、両モデルの結果と人手評価を恒久的なevaluation.mdへ記録する。\n")


def run(*, model=GEMINI_MODEL, execute=False, spec_confirmed=False, run_name=None,
        env=None, logs_dir=weekly.ROOT / "logs", output_root=OUTPUT_ROOT):
    env = os.environ if env is None else env
    name = run_name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if model not in MODELS or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
        print("ERROR: invalid model or run name; API requests: 0", file=sys.stderr)
        return 2
    metrics = {"week": WEEK, "model": model, "api_requests": 0, "http_status": None,
               "elapsed_seconds": 0, "finish_reason": "not_run", "usage": {},
               "status": "not_run", "stage": "prepared", "validation": "not_run",
               "spec_confirmed": bool(spec_confirmed)}
    try:
        week, articles, manifest = prepare(logs_dir)
        out = output_root / model / name
        out.mkdir(parents=True, exist_ok=False)
        plamo.write_json(out / "input-manifest.json", manifest)
        save_results(out, metrics)
    except (OSError, weekly.WeeklyError):
        print("ERROR: fixed W43 input or new output directory check failed; API requests: 0", file=sys.stderr)
        return 2
    result = 0
    if execute:
        key_name = "GEMINI_API_KEY" if model == GEMINI_MODEL else "PLAMO_API_KEY"
        key = (env.get(key_name) or "").strip()
        if not spec_confirmed or not key:
            metrics.update(status="blocked", stage="preflight",
                           error="spec_unconfirmed" if not spec_confirmed else "missing_key")
            message = "official specification confirmation is required" if not spec_confirmed else f"{key_name} is not set"
            print(f"ERROR: {message}; API requests: 0", file=sys.stderr)
            result = 2
        else:
            metrics.update(status="failed", stage="request")
            started = time.monotonic()
            try:
                # Reuse request formats only; weekly.request_gemini retries and must NOT be used here.
                request = (weekly.build_request(week, articles, key, GEMINI_MODEL) if model == GEMINI_MODEL
                           else plamo.build_request(week, articles, key))
                metrics["api_requests"] = 1
                with plamo.open_once(request) as response:
                    metrics["http_status"] = response.status
                    if response.status != 200:
                        raise weekly.ValidationError("unexpected HTTP status")
                    raw = response.read(weekly.MAX_RESPONSE_BYTES + 1)
                metrics["stage"] = "response_json"
                if len(raw) > weekly.MAX_RESPONSE_BYTES:
                    raise weekly.ValidationError("oversized response")
                data = json.loads(raw)
                if not isinstance(data, dict) or data.get("error"):
                    raise weekly.ValidationError("invalid response envelope")
                metrics["stage"] = "response_content"
                metrics["finish_reason"] = finish_reason(data, model)
                usage = weekly.usage_tokens(data) if model == GEMINI_MODEL else plamo.usage_tokens(data)
                metrics["usage"] = {k: v for k, v in usage.items() if type(v) is int and v >= 0}
                if model == PLAMO_MODEL and data.get("model") != PLAMO_MODEL:
                    raise weekly.ValidationError("unexpected PLaMo model")
                text = weekly.extract_text(data) if model == GEMINI_MODEL else plamo.extract_text(data)
                if key in text:
                    raise weekly.ValidationError("credential in output")
                metrics["stage"] = "review_validation"
                review = plamo.validate_output(text, {a.id for a in articles})
                metrics["validation"] = "passed"
                metrics["stage"] = "save_candidate"
                weekly.write_atomic(out / "candidate.md", weekly.render_markdown(
                    week, review, {a.id: a for a in articles}, len(articles),
                    datetime.now(weekly.JST).date(), model))
                metrics.update(status="succeeded", stage="completed")
            except urllib.error.HTTPError as error:
                metrics.update(http_status=error.code, error="http_error")
                error.close()  # Never read or save raw API error bodies.
                result = 1
            except (ValueError, weekly.ValidationError):
                metrics.update(validation="failed", error="invalid_output")
                result = 1
            except (OSError, http.client.HTTPException):
                metrics["error"] = "transport_or_io_error"
                result = 1
            finally:
                metrics["elapsed_seconds"] = round(time.monotonic() - started, 3)
    try:
        save_results(out, metrics)
    except OSError:
        print("ERROR: cannot save run results; do not automatically repeat the API request", file=sys.stderr)
        return 1
    print(f"Model: {model}; API requests: {metrics['api_requests']}; status: {metrics['status']}; "
          f"validation: {metrics['validation']}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODELS, default=GEMINI_MODEL)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--spec-confirmed", action="store_true")
    parser.add_argument("--run-name")
    args = parser.parse_args()
    return run(model=args.model, execute=args.execute, spec_confirmed=args.spec_confirmed, run_name=args.run_name)


if __name__ == "__main__":
    raise SystemExit(main())
