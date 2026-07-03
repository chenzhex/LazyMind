#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import posixpath
import re
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from zipfile import ZipFile


DEFAULT_CHAT_URL = "http://127.0.0.1:8090/api/chat/stream"
DEFAULT_JUDGE_BASE_URL = os.getenv("JUDGE_BASE_URL", "")
DEFAULT_JUDGE_MODEL = os.getenv("JUDGE_MODEL", "")
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
    args.auth_token = args.auth_token or login(args)
    cases = load_cases(args)
    if args.limit > 0:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("no usable eval cases found")
    for index, case in enumerate(cases):
        case["_order"] = index

    if args.serial:
        baseline = [] if args.skip_baseline else run_answers_serial(
            cases,
            args,
            label="baseline",
            algorithm_id=args.baseline_algorithm_id,
            case_timeout_s=args.case_timeout,
        )
        candidate = [] if args.skip_candidate else run_answers_serial(
            cases,
            args,
            label="candidate",
            algorithm_id=args.candidate_algorithm_id,
            case_timeout_s=args.case_timeout,
        )
    else:
        baseline = [] if args.skip_baseline else run_answers(cases, args, label="baseline", algorithm_id=args.baseline_algorithm_id)
        candidate = [] if args.skip_candidate else run_answers(
            cases,
            args,
            label="candidate",
            algorithm_id=args.candidate_algorithm_id,
        )
    rows = merge_rows(cases, baseline, candidate)
    if not args.skip_judge:
        rows = run_judges_serial(rows, args, judge_timeout_s=args.judge_case_timeout) if args.serial else run_judges(rows, args)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    detail_path = args.output_dir / f"{args.run_name}.csv"
    summary_path = args.output_dir / f"{args.run_name}.summary.json"
    write_detail_csv(
        detail_path,
        rows,
        judge_enabled=not args.skip_judge,
        baseline_enabled=not args.skip_baseline,
        candidate_enabled=not args.skip_candidate,
    )
    summary = build_summary(
        rows,
        args,
        judge_enabled=not args.skip_judge,
        baseline_enabled=not args.skip_baseline,
        candidate_enabled=not args.skip_candidate,
    )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"detail_csv={detail_path}")
    print(f"summary_json={summary_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate baseline and evo-optimized LazyMind algorithms on a benchmark-owned eval set."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--eval-file", type=Path, help="CSV/JSON/JSONL/XLSX eval set with question and ground_truth")
    source.add_argument("--eval-set-id", help="LazyMind eval_set_id already imported into /api/core/eval-sets")
    parser.add_argument("--dataset-id", required=True, help="LazyMind dataset/kb id, e.g. ds_xxx")
    parser.add_argument("--chat-url", default=os.getenv("LAZYMIND_EVAL_CHAT_URL", DEFAULT_CHAT_URL))
    parser.add_argument("--baseline-algorithm-id", default="", help="Empty means current default algorithm")
    parser.add_argument("--candidate-algorithm-id", default=os.getenv("LAZYMIND_EVAL_CANDIDATE_ALGORITHM_ID", ""))
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--skip-candidate", action="store_true")
    parser.add_argument("--models", default=os.getenv("LAZYMIND_TEST_MODELS", "LazyMind"))
    parser.add_argument("--llm-config", default=os.getenv("LAZYMIND_TEST_LLM_CONFIG", ""))
    parser.add_argument("--auth-token", default=os.getenv("LAZYMIND_AUTH_TOKEN", ""))
    parser.add_argument("--auth-username", default=os.getenv("LAZYMIND_AUTH_USERNAME", "admin"))
    parser.add_argument("--auth-password", default=os.getenv("LAZYMIND_AUTH_PASSWORD", "admin"))
    parser.add_argument("--user-id", default=os.getenv("LAZYMIND_TEST_USER_ID", ""))
    parser.add_argument("--user-name", default=os.getenv("LAZYMIND_TEST_USER_NAME", ""))
    parser.add_argument("--workers", type=int, default=int(os.getenv("LAZYMIND_EVAL_WORKERS", "2")))
    parser.add_argument("--judge-workers", type=int, default=int(os.getenv("LAZYMIND_JUDGE_WORKERS", "4")))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("LAZYMIND_EVAL_TIMEOUT", "180")))
    parser.add_argument("--judge-timeout", type=int, default=int(os.getenv("JUDGE_TIMEOUT_S", "120")))
    parser.add_argument("--serial", action="store_true", help="Run cases sequentially with hard wall-clock timeouts.")
    parser.add_argument(
        "--case-timeout",
        type=int,
        default=int(os.getenv("LAZYMIND_EVAL_CASE_TIMEOUT", "120")),
        help="Hard timeout in seconds for each chat request when --serial is enabled.",
    )
    parser.add_argument(
        "--judge-case-timeout",
        type=int,
        default=int(os.getenv("LAZYMIND_JUDGE_CASE_TIMEOUT", "60")),
        help="Hard timeout in seconds for each judge request when --serial is enabled.",
    )
    parser.add_argument("--judge-base-url", default=DEFAULT_JUDGE_BASE_URL)
    parser.add_argument("--judge-api-key", default=os.getenv("JUDGE_API_KEY", ""))
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument(
        "--judge-prompt-style",
        choices=("rubric", "original"),
        default=os.getenv("JUDGE_PROMPT_STYLE", "rubric"),
        help="Use 'original' to match the RepLiQA test.py accuracy prompt.",
    )
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument(
        "--no-reference-doc-filter",
        action="store_true",
        help="Only filter by kb_id, matching scripts/generate_chat_eval_jsonl.py.",
    )
    parser.add_argument(
        "--force-kb-instruction",
        action="store_true",
        help="Wrap each benchmark question with an instruction to use semantic KB search and avoid clarification.",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--eval-db-dsn",
        default=os.getenv("LAZYMIND_EVAL_DB_DSN", ""),
        help="Optional psql DSN for reading eval_set_items directly, used only with --eval-set-id",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("eval_owned_set_results"))
    parser.add_argument("--run-name", default=f"owned-set-ab-{time.strftime('%Y%m%d-%H%M%S')}")
    args = parser.parse_args()
    if args.candidate_algorithm_id and is_core_conversation_url(args.chat_url):
        raise SystemExit("--candidate-algorithm-id requires /api/chat/stream, not /api/core/conversations:chat")
    if args.skip_baseline and args.skip_candidate:
        raise SystemExit("cannot use both --skip-baseline and --skip-candidate")
    if not args.skip_candidate and not args.candidate_algorithm_id:
        raise SystemExit("provide --candidate-algorithm-id or use --skip-candidate")
    if not args.skip_judge and (not args.judge_base_url or not args.judge_api_key or not args.judge_model):
        raise SystemExit("judge requires --judge-base-url, --judge-api-key, and --judge-model, or use --skip-judge")
    return args


def load_cases(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = load_eval_set_db(args) if args.eval_set_id and args.eval_db_dsn else (
        load_eval_set_api(args) if args.eval_set_id else load_eval_file(args.eval_file)
    )
    cases: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        question = first_text(row, "question", "query", "prompt", "Question")
        ground_truth = first_text(
            row,
            "ground_truth",
            "groundtruth",
            "answer",
            "expected_answer",
            "reference_answer",
            "Answer",
        )
        if not question or not ground_truth:
            continue
        judge_rubric = first_text(row, "judge_rubric", "judge_rubic", "grading_guidance", "rubric") or ground_truth
        cases.append({
            "case_id": first_text(row, "case_id", "id") or f"case_{index:04d}",
            "question": question,
            "ground_truth": ground_truth,
            "judge_rubric": judge_rubric,
            "key_point": first_text(row, "key_point", "key_points"),
            "expected_refusal": first_text(row, "expected_refusal"),
            "domain": first_text(row, "domain", "kb"),
            "paper_doi": first_text(row, "paper_doi", "doi"),
            "question_type": first_text(row, "question_type", "type", "category"),
            "question_type_str": first_text(row, "question_type_str"),
            "reference_context": first_text(row, "reference_context", "context", "evidence"),
            "reference_doc": first_text(row, "reference_doc", "document", "source"),
            "reference_doc_ids": first_text(row, "reference_doc_ids", "doc_ids"),
            "reference_chunk_ids": first_text(row, "reference_chunk_ids", "chunk_ids"),
        })
    return cases


def load_eval_file(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    if suffix == ".jsonl":
        with path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        for key in ("items", "cases", "data", "rows"):
            if isinstance(data, dict) and isinstance(data.get(key), list):
                return [row for row in data[key] if isinstance(row, dict)]
        raise ValueError(f"unsupported JSON eval set shape: {path}")
    if suffix == ".xlsx":
        return read_xlsx_dicts(path)
    raise ValueError(f"unsupported eval file type: {path}")


def load_eval_set_db(args: argparse.Namespace) -> list[dict[str, Any]]:
    sql = """
select
  coalesce(case_id, '') as case_id,
  question,
  ground_truth,
  coalesce(question_type, '') as question_type,
  coalesce(reference_context, '') as reference_context,
  coalesce(reference_doc, '') as reference_doc,
  coalesce(reference_doc_ids, '') as reference_doc_ids,
  coalesce(reference_chunk_ids, '') as reference_chunk_ids
from eval_set_items
where eval_set_id = :'eval_set_id' and coalesce(is_deleted, false) = false
order by created_at asc, id asc
""".strip()
    proc = subprocess.run(
        [
            "psql",
            args.eval_db_dsn,
            "-v",
            f"eval_set_id={args.eval_set_id}",
            "-At",
            "-F",
            "\t",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = [
        "case_id",
        "question",
        "ground_truth",
        "question_type",
        "reference_context",
        "reference_doc",
        "reference_doc_ids",
        "reference_chunk_ids",
    ]
    return [dict(zip(fields, line.split("\t"))) for line in proc.stdout.splitlines() if line.strip()]


def load_eval_set_api(args: argparse.Namespace) -> list[dict[str, Any]]:
    origin = api_origin(args.chat_url)
    page, rows = 1, []
    while True:
        url = f"{origin}/api/core/eval-sets/{urllib.parse.quote(args.eval_set_id)}/items?page={page}&page_size=100"
        body = get_json(url, headers(args), args.timeout)
        data = unwrap_response(body)
        items = data.get("items") if isinstance(data, dict) else []
        rows.extend([item for item in items or [] if isinstance(item, dict)])
        total = int(data.get("total") or len(rows)) if isinstance(data, dict) else len(rows)
        if len(rows) >= total or not items:
            return rows
        page += 1


def run_answers(cases: list[dict[str, Any]], args: argparse.Namespace, *, label: str, algorithm_id: str) -> list[dict[str, Any]]:
    print(f"running {label}: cases={len(cases)} workers={args.workers} algorithm_id={algorithm_id or '<default>'}", flush=True)
    out: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(answer_one, case, args, label=label, algorithm_id=algorithm_id): case
            for case in cases
        }
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            out.append(row)
            mark = "ok" if not row.get("error") else "fail"
            print(f"{label} {index}/{len(cases)} {mark} {row['case_id']}", flush=True)
    return sorted(out, key=lambda item: item["_order"])


def run_answers_serial(
    cases: list[dict[str, Any]],
    args: argparse.Namespace,
    *,
    label: str,
    algorithm_id: str,
    case_timeout_s: int,
) -> list[dict[str, Any]]:
    print(f"running {label} (serial): cases={len(cases)} timeout={case_timeout_s}s algorithm_id={algorithm_id or '<default>'}", flush=True)
    out: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        row = answer_one_serial(case, args, label=label, algorithm_id=algorithm_id, case_timeout_s=case_timeout_s)
        out.append(row)
        mark = "ok" if not row.get("error") else "fail"
        print(f"{label} {index}/{len(cases)} {mark} {row['case_id']}", flush=True)
    return sorted(out, key=lambda item: item["_order"])


def answer_one(case: dict[str, Any], args: argparse.Namespace, *, label: str, algorithm_id: str) -> dict[str, Any]:
    return answer_one_with_timeout(case, args, label=label, algorithm_id=algorithm_id, case_timeout_s=None)


def answer_one_with_timeout(
    case: dict[str, Any],
    args: argparse.Namespace,
    *,
    label: str,
    algorithm_id: str,
    case_timeout_s: int | None,
) -> dict[str, Any]:
    started = time.time()
    payload = build_chat_payload(case, args, algorithm_id)
    try:
        if case_timeout_s is not None:
            with hard_timeout(case_timeout_s, name=f"{label} chat case {case['case_id']}"):
                answer = post_chat_stream(args.chat_url, payload, headers(args), args.timeout)
        else:
            answer = post_chat_stream(args.chat_url, payload, headers(args), args.timeout)
        return {
            "_order": int(case["_order"]),
            "case_id": case["case_id"],
            "label": label,
            "algorithm_id": algorithm_id,
            "answer": answer,
            "cost_s": round(time.time() - started, 3),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 - batch runner records per-case failures.
        return {
            "_order": int(case["_order"]),
            "case_id": case["case_id"],
            "label": label,
            "algorithm_id": algorithm_id,
            "answer": "",
            "cost_s": round(time.time() - started, 3),
            "error": str(exc),
        }


def answer_one_serial(case: dict[str, Any], args: argparse.Namespace, *, label: str, algorithm_id: str, case_timeout_s: int) -> dict[str, Any]:
    return answer_one_with_timeout(case, args, label=label, algorithm_id=algorithm_id, case_timeout_s=case_timeout_s)


def build_chat_payload(case: dict[str, Any], args: argparse.Namespace, algorithm_id: str) -> dict[str, Any]:
    question = build_chat_question(case["question"], args)
    if is_core_conversation_url(args.chat_url):
        return {
            "conversation_id": str(uuid.uuid4()),
            "conversation": {"search_config": {"dataset_list": [{"id": args.dataset_id}], "database_ids": []}},
            "models": [item.strip() for item in args.models.split(",") if item.strip()],
            "stream": True,
            "reasoning": False,
            "input": [{"input_type": "text", "text": question}],
            "environment_context": {},
        }
    payload: dict[str, Any] = {
        "query": question,
        "history": [],
        "session_id": f"owned-eval-{case['case_id']}-{uuid.uuid4().hex[:8]}",
        "dataset": args.dataset_id,
        "filters": build_kb_filters(case, args),
        "trace": False,
        "reasoning": False,
        "disabled_tools": list(DISABLED_CHAT_TOOLS),
    }
    if algorithm_id:
        payload["algorithm_id"] = algorithm_id
    if args.llm_config:
        payload["llm_config"] = json.loads(args.llm_config)
    return payload


def build_chat_question(question: str, args: argparse.Namespace) -> str:
    if not args.force_kb_instruction:
        return question
    return f"""
Answer the benchmark question using only the configured knowledge base.
You must use semantic knowledge-base search first with the benchmark question and its named entities.
Do not use document-scoped keyword or file-name search.
Do not ask for clarification; if the question is grammatically awkward, infer the intended meaning from retrieved evidence.
Return only the final answer.

Benchmark question:
{question}
""".strip()


def build_kb_filters(case: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    filters: dict[str, Any] = {"kb_id": [args.dataset_id]}
    if args.no_reference_doc_filter:
        return filters
    file_names = parse_string_list(case.get("reference_doc"))
    if file_names:
        filters["file_name"] = file_names
    doc_ids = parse_string_list(case.get("reference_doc_ids"))
    if doc_ids:
        filters["docid"] = doc_ids
    return filters


def parse_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text_value = str(value).strip()
    if not text_value:
        return []
    try:
        parsed = json.loads(text_value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [text_value]


def merge_rows(
    cases: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_baseline = {row["case_id"]: row for row in baseline}
    by_candidate = {row["case_id"]: row for row in candidate}
    rows = []
    for case in cases:
        base = by_baseline.get(case["case_id"], {})
        cand = by_candidate.get(case["case_id"], {})
        rows.append({
            **case,
            "baseline_answer": base.get("answer", ""),
            "baseline_cost_s": base.get("cost_s", ""),
            "baseline_error": base.get("error", ""),
            "candidate_answer": cand.get("answer", ""),
            "candidate_cost_s": cand.get("cost_s", ""),
            "candidate_error": cand.get("error", ""),
        })
    return rows


def run_judges(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    print(f"judging: cases={len(rows)} workers={args.judge_workers}", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.judge_workers)) as executor:
        futures = [executor.submit(judge_row, row, args) for row in rows]
        judged = []
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            judged.append(row)
            print(
                "judge "
                f"{index}/{len(rows)} "
                f"case={row['case_id']} "
                f"base={row.get('baseline_score', '')} cand={row.get('candidate_score', '')}",
                flush=True,
            )
    return sorted(judged, key=lambda item: item["_order"])


def run_judges_serial(rows: list[dict[str, Any]], args: argparse.Namespace, *, judge_timeout_s: int) -> list[dict[str, Any]]:
    print(f"judging (serial): cases={len(rows)} timeout={judge_timeout_s}s", flush=True)
    judged = []
    for index, row in enumerate(rows, 1):
        row = judge_row_with_timeout(row, args, judge_timeout_s=judge_timeout_s)
        judged.append(row)
        scores = []
        if not args.skip_baseline:
            scores.append(f"base={row.get('baseline_score', '')}")
        if not args.skip_candidate:
            scores.append(f"cand={row.get('candidate_score', '')}")
        print(
            "judge "
            f"{index}/{len(rows)} "
            f"case={row['case_id']} "
            + " ".join(scores),
            flush=True,
        )
    return sorted(judged, key=lambda item: item["_order"])


def judge_row(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    return judge_row_with_timeout(row, args, judge_timeout_s=None)


def judge_row_with_timeout(row: dict[str, Any], args: argparse.Namespace, *, judge_timeout_s: int | None) -> dict[str, Any]:
    row = dict(row)
    if not args.skip_baseline:
        row["baseline_score"], row["baseline_judge_error"] = score_answer(
            row,
            "baseline_answer",
            args,
            judge_timeout_s=judge_timeout_s,
        )
    if args.skip_candidate:
        return row
    if row.get("candidate_answer"):
        row["candidate_score"], row["candidate_judge_error"] = score_answer(
            row,
            "candidate_answer",
            args,
            judge_timeout_s=judge_timeout_s,
        )
        if not args.skip_baseline:
            row["score_delta"] = round(row["candidate_score"] - row["baseline_score"], 4)
            row["outcome"] = outcome(row["score_delta"])
    else:
        row["candidate_score"], row["candidate_judge_error"] = 0.0, row.get("candidate_error") or "missing candidate"
        if not args.skip_baseline:
            row["score_delta"], row["outcome"] = round(-row["baseline_score"], 4), "regressed"
    return row


def score_answer(
    row: dict[str, Any],
    answer_key: str,
    args: argparse.Namespace,
    *,
    judge_timeout_s: int | None,
) -> tuple[float, str]:
    if answer_key == "baseline_answer" and row.get("baseline_error"):
        return 0.0, f"skip judge because chat failed: {row['baseline_error']}"
    if answer_key == "candidate_answer" and row.get("candidate_error"):
        return 0.0, f"skip judge because chat failed: {row['candidate_error']}"
    prompt = judge_prompt(
        row["question"],
        row["ground_truth"],
        row.get(answer_key, ""),
        row.get("judge_rubric", ""),
        style=args.judge_prompt_style,
    )
    payload = {
        "model": args.judge_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "top_p": 1,
    }
    req_headers = {"Authorization": f"Bearer {args.judge_api_key}", "Content-Type": "application/json"}
    url = args.judge_base_url.rstrip("/") + "/chat/completions"
    try:
        if judge_timeout_s is not None:
            with hard_timeout(judge_timeout_s, name=f"judge case {row['case_id']} {answer_key}"):
                body = post_json(url, payload, req_headers, args.judge_timeout)
        else:
            body = post_json(url, payload, req_headers, args.judge_timeout)
        content = body["choices"][0]["message"]["content"].strip()
        return parse_judge_score(content), ""
    except Exception as exc:  # noqa: BLE001 - batch runner records per-case failures.
        return 0.0, str(exc)


def judge_prompt(question: str, ground_truth: str, answer: str, judge_rubric: str, *, style: str) -> str:
    if style == "original":
        return original_judge_prompt(question, ground_truth, answer)
    return f"""
You are a strict ResearchQA evaluation judge.

Score only answer quality using the question, the ground-truth answer, and the judge rubric.
Do not reward fluent but unsupported answers. For adversarial or false-premise cases,
answers that correctly refuse to fabricate should score highly when the rubric requires refusal.

Score scale:
1.0 = Fully satisfies the ground truth and every important rubric criterion.
0.8 = Correct answer with only minor omissions or wording differences.
0.6 = Mostly correct but misses a non-critical rubric criterion.
0.4 = Partially correct but misses important facts or rubric requirements.
0.2 = Barely related, or has major factual/rubric errors.
0.0 = Wrong, fabricated, unsupported, or does not answer the question.

Output exactly one number from: 0.0, 0.2, 0.4, 0.6, 0.8, 1.0.
Do not output reasoning, JSON, markdown, tags, or extra text.

Question:
{question}

Ground truth:
{ground_truth}

Judge rubric:
{judge_rubric or "(No separate rubric provided; use the ground truth as the rubric.)"}

Answer to evaluate:
{answer}

Score:
""".strip()


def original_judge_prompt(question: str, ground_truth: str, answer: str) -> str:
    return f"""
【任务：严格答案准确率评分】
【全局唯一规则，所有样本统一标准，禁止松紧不一致】
本评分仅基于**事实性内容匹配度**，不考量语言流畅性、表述风格、逻辑连贯性等非事实维度，具体分级标准如下：
1.0 = 答案与标准答案核心信息完全一致
0.8 = 答案正确，仅表述不同
0.6 = 答案主体正确，少量细节缺失
0.4 = 答案部分正确，关键信息有缺失
0.2 = 答案沾边，但核心错误
0.0 = 完全错误、答非所问

【输出强制要求】
- 只输出 0.0、0.2、0.4、0.6、0.8、1.0 其中一个浮点数
- 不输出任何文字、标点、空格、解释
- 全程保持统一评分尺度，不产生漂移

【问题】
{question}

【标准答案】
{ground_truth}

【待评估答案】
{answer}

【分数】
""".strip()


def parse_judge_score(content: str) -> float:
    cleaned = re.sub(r"<think\b[^>]*>.*?</think>", "", content, flags=re.S | re.I).strip()
    allowed = {0.0, 0.2, 0.4, 0.6, 0.8, 1.0}
    candidates = re.findall(r"(?<!\d)(?:0(?:\.[02468])?|1(?:\.0)?)(?!\d)", cleaned)
    if not candidates:
        candidates = re.findall(r"(?<!\d)(?:0(?:\.[02468])?|1(?:\.0)?)(?!\d)", content)
    if not candidates:
        raise ValueError(f"judge did not return a valid score: {content[:200]}")
    score = float(candidates[-1])
    if score not in allowed:
        raise ValueError(f"judge returned unsupported score: {score}")
    return max(0.0, min(1.0, score))


def build_summary(
    rows: list[dict[str, Any]],
    args: argparse.Namespace,
    *,
    judge_enabled: bool,
    baseline_enabled: bool,
    candidate_enabled: bool,
) -> dict[str, Any]:
    baseline_scores = [float(row.get("baseline_score") or 0.0) for row in rows] if judge_enabled and baseline_enabled else []
    candidate_scores = [float(row.get("candidate_score") or 0.0) for row in rows] if judge_enabled and candidate_enabled else []
    compare_enabled = baseline_enabled and candidate_enabled
    return {
        "run_name": args.run_name,
        "dataset_id": args.dataset_id,
        "eval_source": str(args.eval_file or args.eval_set_id),
        "case_count": len(rows),
        "baseline_algorithm_id": args.baseline_algorithm_id or "<default>" if baseline_enabled else "",
        "candidate_algorithm_id": args.candidate_algorithm_id if candidate_enabled else "",
        "judge_enabled": judge_enabled,
        "baseline_avg": avg(baseline_scores),
        "candidate_avg": avg(candidate_scores),
        "delta_avg": round(avg(candidate_scores) - avg(baseline_scores), 4) if compare_enabled and candidate_scores else None,
        "improved_count": sum(row.get("outcome") == "improved" for row in rows) if compare_enabled else 0,
        "regressed_count": sum(row.get("outcome") == "regressed" for row in rows) if compare_enabled else 0,
        "unchanged_count": sum(row.get("outcome") == "unchanged" for row in rows) if compare_enabled else 0,
        "baseline_chat_failures": sum(bool(row.get("baseline_error")) for row in rows) if baseline_enabled else 0,
        "candidate_chat_failures": sum(bool(row.get("candidate_error")) for row in rows) if candidate_enabled else 0,
        "baseline_judge_failures": sum(bool(row.get("baseline_judge_error")) for row in rows) if baseline_enabled else 0,
        "candidate_judge_failures": sum(bool(row.get("candidate_judge_error")) for row in rows) if candidate_enabled else 0,
    }


def write_detail_csv(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    judge_enabled: bool,
    baseline_enabled: bool,
    candidate_enabled: bool,
) -> None:
    fields = [
        "case_id",
        "question",
        "ground_truth",
        "judge_rubric",
        "key_point",
        "expected_refusal",
        "domain",
        "paper_doi",
        "question_type",
        "question_type_str",
        "reference_context",
        "reference_doc",
        "reference_doc_ids",
        "reference_chunk_ids",
        "baseline_answer",
        "baseline_cost_s",
        "baseline_error",
        "candidate_answer",
        "candidate_cost_s",
        "candidate_error",
    ]
    if judge_enabled:
        fields.extend([
            "baseline_score",
            "baseline_judge_error",
            "candidate_score",
            "candidate_judge_error",
            "score_delta",
            "outcome",
        ])
    if not baseline_enabled:
        fields = [field for field in fields if not field.startswith("baseline_") and field not in {"score_delta", "outcome"}]
    if not candidate_enabled:
        fields = [field for field in fields if not field.startswith("candidate_") and field not in {"score_delta", "outcome"}]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def login(args: argparse.Namespace) -> str:
    if not needs_auth(args.chat_url):
        return ""
    origin = api_origin(args.chat_url)
    payload = {"username": args.auth_username, "password": args.auth_password}
    body = post_json(
        f"{origin}/api/authservice/auth/login",
        payload,
        {"Content-Type": "application/json", "Accept": "application/json"},
        args.timeout,
    )
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    token = data.get("access_token") if isinstance(data, dict) else ""
    if not token:
        raise RuntimeError("login response missing access_token")
    return token


def headers(args: argparse.Namespace) -> dict[str, str]:
    out = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if args.auth_token:
        out["Authorization"] = args.auth_token if args.auth_token.startswith("Bearer ") else f"Bearer {args.auth_token}"
    if args.user_id:
        out["X-User-Id"] = args.user_id
    if args.user_name:
        out["X-User-Name"] = args.user_name
    return out


def post_chat_stream(url: str, payload: dict[str, Any], req_headers: dict[str, str], timeout: int) -> str:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    parts: list[str] = []
    final_message = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = b""
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                raw += raw_line
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
            if not parts and not final_message:
                parsed = parse_non_stream_body(raw.decode("utf-8", errors="replace"))
                parts.extend(parsed)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    answer = clean_stream_answer("".join(parts)) or clean_stream_answer(final_message)
    if not answer:
        raise RuntimeError("chat finished without answer text")
    invalid_reason = invalid_chat_answer_reason(answer)
    if invalid_reason:
        raise RuntimeError(invalid_reason)
    return answer


def invalid_chat_answer_reason(answer: str) -> str:
    lowered = answer.lower()
    patterns = (
        "knowledge base search failed",
        "kb search is currently unavailable",
        "kb search is unavailable",
        "search failed due to a connection error",
        "search failed due to an internal server error",
        "search failed due to a service error",
        "internal server error (500)",
        "service error",
        "connection error",
        "need to clarify your question",
        "appears grammatically incomplete or ambiguous",
        "could you please rephrase",
        "please rephrase",
        "let me know so i can help accurately",
        "the knowledge base search did not return any relevant information",
        "the knowledge base search did not return any results",
        "the search results are irrelevant",
        "i cannot answer the benchmark question",
        "i cannot provide an answer based on the configured knowledge base",
        "i cannot answer the question based on the available knowledge base",
        "i have not learned how to answer this question yet",
    )
    for pattern in patterns:
        if pattern in lowered:
            return f"chat returned infrastructure failure text: {pattern}"
    return ""


def post_json(url: str, payload: dict[str, Any], req_headers: dict[str, str], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


@contextmanager
def hard_timeout(seconds: int, *, name: str):
    if seconds <= 0:
        yield
        return

    def _raise_timeout(signum: int, frame: Any) -> None:  # noqa: ARG001
        raise TimeoutError(f"{name} exceeded hard timeout of {seconds}s")

    previous_handler = signal.signal(signal.SIGALRM, _raise_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0 or previous_timer[1] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def get_json(url: str, req_headers: dict[str, str], timeout: int) -> dict[str, Any]:
    headers_json = dict(req_headers)
    headers_json["Accept"] = "application/json"
    req = urllib.request.Request(url, headers=headers_json, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def read_xlsx_dicts(path: Path) -> list[dict[str, Any]]:
    rows = read_xlsx_rows(path)
    if not rows:
        return []
    headers_row = [str(item).strip() for item in rows[0]]
    out = []
    for row in rows[1:]:
        out.append({headers_row[index]: row[index] if index < len(row) else "" for index in range(len(headers_row))})
    return out


def read_xlsx_rows(path: Path) -> list[list[str]]:
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(path) as zf:
        shared = shared_strings(zf, ns)
        for sheet_path in iter_xlsx_sheet_paths(zf):
            rows = read_xlsx_sheet_rows(zf, sheet_path, shared, ns)
            if rows and xlsx_rows_look_like_eval_set(rows):
                return rows
        for sheet_path in iter_xlsx_sheet_paths(zf):
            rows = read_xlsx_sheet_rows(zf, sheet_path, shared, ns)
            if rows:
                return rows
        return []


def iter_xlsx_sheet_paths(zf: ZipFile) -> list[str]:
    if "xl/workbook.xml" not in zf.namelist() or "xl/_rels/workbook.xml.rels" not in zf.namelist():
        return [name for name in zf.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")]
    ns = {
        "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rels = {rel.attrib.get("Id", ""): rel.attrib.get("Target", "") for rel in rels_root}
    paths: list[str] = []
    for sheet in workbook.findall("a:sheets/a:sheet", ns):
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
        target = rels.get(rel_id, "")
        if not target:
            continue
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = posixpath.normpath(posixpath.join("xl", target))
        paths.append(target)
    return paths


def read_xlsx_sheet_rows(zf: ZipFile, sheet_name: str, shared: list[str], ns: dict[str, str]) -> list[list[str]]:
    sheet = ET.fromstring(zf.read(sheet_name))
    rows: list[list[str]] = []
    for row in sheet.findall("a:sheetData/a:row", ns):
        values: dict[int, str] = {}
        for cell in row.findall("a:c", ns):
            col = column_index(cell.attrib.get("r", "A1"))
            values[col] = cell_value(cell, shared, ns)
        if values:
            rows.append([values.get(i, "") for i in range(max(values) + 1)])
    return rows


def xlsx_rows_look_like_eval_set(rows: list[list[str]]) -> bool:
    if not rows:
        return False
    headers = {normalize_header(value) for value in rows[0] if normalize_header(value)}
    return {"question", "groundtruth"}.issubset(headers) or {"question", "ground_truth"}.issubset(headers)


def normalize_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def shared_strings(zf: ZipFile, ns: dict[str, str]) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.findall(".//a:t", ns)) for si in root.findall("a:si", ns)]


def cell_value(cell: ET.Element, shared: list[str], ns: dict[str, str]) -> str:
    typ = cell.attrib.get("t")
    if typ == "inlineStr":
        return "".join(t.text or "" for t in cell.findall(".//a:t", ns)).strip()
    node = cell.find("a:v", ns)
    if node is None or node.text is None:
        return ""
    value = node.text
    if typ == "s":
        return shared[int(value)].strip()
    return value.strip()


def column_index(ref: str) -> int:
    col = 0
    for char in ref:
        if not char.isalpha():
            break
        col = col * 26 + ord(char.upper()) - ord("A") + 1
    return col - 1


def parse_non_stream_body(raw: str) -> list[str]:
    if not raw.strip():
        return []
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    if not is_success(body):
        raise RuntimeError(body.get("msg") or body.get("message") or body)
    return extract_texts(body)


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


def extract_texts(body: dict[str, Any]) -> list[str]:
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("answer", "text", "message", "data"):
            value = data.get(key)
            if isinstance(value, str):
                return [value]
    if isinstance(data, str):
        return [data]
    value = body.get("answer")
    return [value] if isinstance(value, str) else []


def clean_stream_answer(value: str) -> str:
    return re.sub(r"<(?:tp|trp|tool_call|tool_result)\b[^>]*>.*?</(?:tp|trp|tool_call|tool_result)>", "", value, flags=re.S).strip()


def is_success(body: dict[str, Any]) -> bool:
    return body.get("code") in (None, 0, 200)


def unwrap_response(body: dict[str, Any]) -> Any:
    if isinstance(body, dict) and "data" in body and body.get("code") in (0, 200, None):
        return body["data"]
    return body


def first_text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        text = str(value).strip()
        if text:
            return text
    return ""


def avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def outcome(delta: float) -> str:
    if delta > 0.0001:
        return "improved"
    if delta < -0.0001:
        return "regressed"
    return "unchanged"


def api_origin(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def is_core_conversation_url(url: str) -> bool:
    return "/api/core/conversations:chat" in url or url.rstrip("/").endswith("/conversations:chat")


def needs_auth(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == 8090


if __name__ == "__main__":
    raise SystemExit(main())
