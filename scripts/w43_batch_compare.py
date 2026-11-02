#!/usr/bin/env python3
"""Offline preparation / credential gate / summary for the two-step W43 workflow."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import w43_api_compare as compare


def batch_dir(root, name):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
        raise ValueError("invalid run name")
    return root / "comparison" / name


def read_json(path):
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("expected object")
    return data


def prepare(name, *, execute=False, spec_confirmed=False, root=compare.OUTPUT_ROOT,
            logs_dir=compare.weekly.ROOT / "logs"):
    out = batch_dir(root, name)
    out.mkdir(parents=True, exist_ok=False)
    state = {"started_at": datetime.now(timezone.utc).isoformat(), "execute": execute,
             "spec_confirmed": spec_confirmed, "status": "blocked", "error": "input_mismatch",
             "inputs_match": False, "manifests": {}}
    try:
        # Prepare BOTH fixed manifests before either API step is allowed to run.
        manifests = {model: compare.prepare(logs_dir)[2] for model in compare.MODELS}
        if manifests[compare.MODELS[0]] != manifests[compare.MODELS[1]]:
            raise ValueError("different input manifests")
        state.update(inputs_match=True, manifests=manifests)
        if any((root / model / name).exists() for model in compare.MODELS):
            state["error"] = "existing_result"
        elif execute and not spec_confirmed:
            state["error"] = "spec_unconfirmed"
        elif not execute:
            for model in compare.MODELS:
                if compare.run(model=model, run_name=name, logs_dir=logs_dir, output_root=root) != 0:
                    raise ValueError("offline preparation failed")
            state.update(status="prepared", error="none")
        else:
            state.update(status="awaiting_credentials", error="none")
    except (OSError, ValueError, compare.weekly.WeeklyError):
        pass  # Fixed diagnostic categories only; no raw error strings.
    compare.plamo.write_json(out / "preflight.json", state)
    return 0 if state["status"] != "blocked" else 2


def check_credentials(name, *, env=None, root=compare.OUTPUT_ROOT):
    env = os.environ if env is None else env
    path = batch_dir(root, name) / "preflight.json"
    state = read_json(path)
    if state.get("status") != "awaiting_credentials" or not state.get("spec_confirmed"):
        return 2
    # This command never calls an API. Values are inspected for presence only.
    ready = all((env.get(key) or "").strip() for key in ("GEMINI_API_KEY", "PLAMO_API_KEY"))
    state.update(status="ready" if ready else "blocked", error="none" if ready else "missing_credentials")
    compare.plamo.write_json(path, state)
    if not ready:
        print("ERROR: both GEMINI_API_KEY and PLAMO_API_KEY are required; API requests: 0", file=sys.stderr)
    return 0 if ready else 2


def safe_row(model, data, candidate):
    # Only known enums / finite numbers reach Markdown or Job Summary.
    def label(key, allowed, fallback="other"):
        value = data.get(key)
        return value if isinstance(value, str) and value in allowed else fallback

    def number(key, integer=False):
        value = data.get(key)
        valid = type(value) is int if integer else type(value) in {int, float}
        return value if valid and value >= 0 and math.isfinite(value) else None

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    usage = {key: value for key, value in usage.items()
             if key in {"prompt", "output", "thoughts", "total", "prompt_tokens", "completion_tokens", "total_tokens"}
             and type(value) is int and value >= 0}
    row = {"model": model, "api_requests": number("api_requests", True),
           "http_status": number("http_status", True), "elapsed_seconds": number("elapsed_seconds"),
           "status": label("status", {"not_run", "blocked", "failed", "succeeded"}),
           "validation": label("validation", {"not_run", "passed", "failed"}),
           "finish_reason": label("finish_reason", {"not_run", "missing", "other", "STOP", "MAX_TOKENS", "SAFETY",
               "RECITATION", "PROHIBITED_CONTENT", "OTHER", "stop", "length", "content_filter", "tool_calls", "function_call"}),
           "error": label("error", {"none", "missing_result", "http_error", "invalid_output", "transport_or_io_error",
               "spec_unconfirmed", "missing_key", "missing_credentials", "preflight_failed", "input_mismatch", "existing_result"},
               "none" if "error" not in data else "other"), "usage": usage}
    row["candidate"] = candidate and row["status"] == "succeeded" and row["validation"] == "passed"
    return row


def summarize(name, *, root=compare.OUTPUT_ROOT, summary_path=None, outcomes=None):
    out = batch_dir(root, name)
    state = read_json(out / "preflight.json")
    outcomes = outcomes or {}
    rows, manifests = [], []
    for model in compare.MODELS:
        folder = root / model / name
        try:
            data = read_json(folder / "metrics.json")
        except (OSError, ValueError):
            blocked = state.get("status") == "blocked"
            data = {"status": "blocked" if blocked else "failed", "api_requests": 0 if blocked else None,
                    "validation": "not_run", "finish_reason": "not_run", "elapsed_seconds": 0,
                    "error": state.get("error", "preflight_failed") if blocked else "missing_result"}
        rows.append(safe_row(model, data, (folder / "candidate.md").is_file()))
        outcome = outcomes.get(model)
        rows[-1]["step_outcome"] = outcome if outcome in {"success", "failure", "skipped", "cancelled"} else "not_reported"
        if outcome == "failure" and rows[-1]["error"] == "none":
            rows[-1]["error"] = "step_failure"
        try:
            manifests.append(read_json(folder / "input-manifest.json"))
        except (OSError, ValueError):
            manifests.append(None)
    prepared = state.get("manifests", {})
    matched = None if any(manifest is None for manifest in manifests) else (state.get("inputs_match") is True and all(
        manifest is not None and manifest == prepared.get(model)
        for model, manifest in zip(compare.MODELS, manifests)))
    successes = [row["status"] == "succeeded" and row["validation"] == "passed" and row["candidate"]
                 and row["api_requests"] == 1 and row["http_status"] == 200
                 and row["step_outcome"] in {"success", "not_reported"} for row in rows]
    if state.get("status") == "blocked":
        status, result = "blocked", 2
    elif not state.get("execute") and matched and all(row["status"] == "not_run" and row["api_requests"] == 0 for row in rows):
        status, result = "prepared", 0
    elif state.get("status") == "ready" and matched and all(successes):
        status, result = "succeeded", 0
    else:
        status, result = "partial_failure" if any(successes) else "failed", 1
    try:
        started_at = datetime.fromisoformat(state["started_at"]).astimezone(timezone.utc).isoformat()
    except (KeyError, ValueError, TypeError):
        started_at = "unknown"
    info = {"status": status, "started_at": started_at, "generated_at": datetime.now(timezone.utc).isoformat(),
            "prepared_inputs_match": state.get("inputs_match") is True, "result_inputs_match": matched,
            "models": rows}
    compare.plamo.write_json(out / "comparison.json", info)
    labels = [("状態", "status"), ("Actionsステップ結果", "step_outcome"), ("API送信試行数", "api_requests"), ("HTTP", "http_status"),
              ("所要秒", "elapsed_seconds"), ("完了理由", "finish_reason"), ("JSON/出典ID検証", "validation"),
              ("usage（モデルごとの定義）", "usage"), ("検証済み候補", "candidate"), ("エラー種別", "error")]
    lines = ["# W43 API comparison", "", f"実行日時（準備開始・UTC）: {info['started_at']}", "",
             f"比較表生成（UTC）: {info['generated_at']}", "",
             f"最終判定: **{status}**", "", f"送信前の両入力一致: {info['prepared_inputs_match']} / "
             f"結果manifestと準備入力の一致: {matched}", "",
             f"| 指標 | {compare.MODELS[0]} | {compare.MODELS[1]} |", "| --- | --- | --- |"]
    for title, key in labels:
        cells = [json.dumps(row[key], ensure_ascii=False) for row in rows]
        lines.append(f"| {title} | {cells[0]} | {cells[1]} |")
    for key in compare.EXPECTED_HASHES:
        # Print only our fixed expected digests; never echo arbitrary manifest values.
        lines += ["", f"固定 {key}: `{compare.EXPECTED_HASHES[key]}`"]
    lines += ["", "自動比較は技術指標のみ。日本語品質・トピック選定・実務への示唆・出典の事実性は、",
              "候補を人が確認しevaluation.mdへ記録する。追加の採点API・本番反映は行わない。", ""]
    text = "\n".join(lines)
    compare.weekly.write_atomic(out / "comparison.md", text)
    if summary_path:
        with summary_path.open("a", encoding="utf-8") as file:
            file.write(text)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "check-credentials", "summarize"))
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--spec-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        if args.phase == "prepare":
            return prepare(args.run_name, execute=args.execute, spec_confirmed=args.spec_confirmed)
        if args.phase == "check-credentials":
            return check_credentials(args.run_name)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        return summarize(args.run_name, summary_path=Path(summary) if summary else None,
                         outcomes={compare.GEMINI_MODEL: os.environ.get("GEMINI_OUTCOME"),
                                   compare.PLAMO_MODEL: os.environ.get("PLAMO_OUTCOME")})
    except (OSError, ValueError):
        print("ERROR: comparison files could not be prepared or read; no API is called by this command", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
