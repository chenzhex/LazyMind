#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd


DISABLED_CHAT_TOOLS = (
    "temp_kb",
    "wikipedia",
    "web_search",
    "academic_search",
    "url_fetch",
    "multimodal",
    "vocab_learn",
    "memory_editor",
    "skill_editor",
    "feishu",
)


def main() -> int:
    args = parse_args()
    cases = load_cases(args.eval_file)
    if args.limit > 0:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("no usable cases found")

    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("w", encoding="utf-8") as out:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(answer_one, case, args): case
                for case in cases
            }
            rows: dict[int, dict[str, Any]] = {}
            for done, future in enumerate(as_completed(futures), 1):
                row = future.result()
                rows[int(row["_order"])] = row
                status = "ok" if not row.get("error") else "fail"
                print(f"{done}/{len(cases)} {status} {row['case_id']} {row.get('cost_s', '')}", flush=True)
            for order in sorted(rows):
                row = rows[order]
                row.pop("_order", None)
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate JSONL answers for the original RepLiQA judge script.")
    parser.add_argument("--eval-file", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--chat-url", default="http://localhost:8046/api/chat/stream")
    parser.add_argument("--algorithm-id", default="")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args()


def load_cases(path: Path) -> list[dict[str, Any]]:
    df = pd.read_excel(path)
    rows: list[dict[str, Any]] = []
    for index, raw in df.iterrows():
        if is_deleted(raw.get("is_delete")):
            continue
        question = text(raw.get("question"))
        ground_truth = text(raw.get("ground_truth"))
        if not question or not ground_truth:
            continue
        rows.append(
            {
                "_order": len(rows),
                "case_id": text(raw.get("case_id")) or text(raw.get("id")) or f"case_{index + 1:04d}",
                "question": question,
                "ground_truth": ground_truth,
                "reference_doc": text(raw.get("reference_doc")),
                "question_type": text(raw.get("question_type")),
                "reference_context": text(raw.get("reference_context")),
                "answer_type": text(raw.get("answer_type")),
                "kb": text(raw.get("kb")),
                "document_id": text(raw.get("document_id")),
            }
        )
    return rows


def answer_one(case: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    payload = {
        "query": case["question"],
        "history": [],
        "session_id": f"repliqa-eval-{case['case_id']}-{uuid.uuid4().hex[:8]}",
        "dataset": args.dataset_id,
        "filters": {"kb_id": [args.dataset_id]},
        "trace": False,
        "reasoning": False,
        "disabled_tools": list(DISABLED_CHAT_TOOLS),
    }
    if args.algorithm_id:
        payload["algorithm_id"] = args.algorithm_id
    row = dict(case)
    try:
        row["answer"] = post_chat_stream(args.chat_url, payload, args.timeout)
        row["error"] = ""
    except Exception as exc:  # noqa: BLE001 - batch runner records per-case failures.
        row["answer"] = ""
        row["error"] = str(exc)
    row["algorithm_id"] = args.algorithm_id or "<default>"
    row["cost_s"] = round(time.time() - started, 3)
    return row


def post_chat_stream(url: str, payload: dict[str, Any], timeout: int) -> str:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    parts: list[str] = []
    final_message = ""
    raw = b""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                raw += raw_line
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    body = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not is_success(body):
                    raise RuntimeError(body.get("msg") or body.get("message") or body)
                texts, message = stream_texts(body)
                parts.extend(texts)
                if message.strip():
                    final_message = message
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    answer = clean_stream_answer("".join(parts)) or clean_stream_answer(final_message)
    if not answer:
        answer = clean_stream_answer(parse_non_stream_body(raw.decode("utf-8", errors="replace")))
    if not answer:
        raise RuntimeError("chat finished without answer text")
    return answer


def stream_texts(body: Any) -> tuple[list[str], str]:
    if isinstance(body, str):
        return [body], ""
    if not isinstance(body, dict):
        return [], ""
    chunk = body.get("result") or body.get("data") or body
    if isinstance(chunk, str):
        return [chunk], ""
    if not isinstance(chunk, dict):
        return [], ""
    parts = [chunk[key] for key in ("delta", "text", "answer") if isinstance(chunk.get(key), str)]
    message = chunk.get("message")
    return parts, message if isinstance(message, str) else ""


def parse_non_stream_body(raw: str) -> str:
    if not raw.strip():
        return ""
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not is_success(body):
        raise RuntimeError(body.get("msg") or body.get("message") or body)
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        for key in ("answer", "text", "message", "data"):
            value = data.get(key)
            if isinstance(value, str):
                return value
    if isinstance(data, str):
        return data
    value = body.get("answer") if isinstance(body, dict) else ""
    return value if isinstance(value, str) else ""


def is_success(body: dict[str, Any]) -> bool:
    return body.get("code") in (None, 0, 200)


def clean_stream_answer(value: str) -> str:
    return re.sub(
        r"<(?:tp|trp|tool_call|tool_result)\b[^>]*>.*?</(?:tp|trp|tool_call|tool_result)>",
        "",
        value,
        flags=re.S,
    ).strip()


def is_deleted(value: Any) -> bool:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


if __name__ == "__main__":
    raise SystemExit(main())
