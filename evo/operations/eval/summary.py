from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

UNSCORED = {'infra_failure', 'judge_contract_error', 'dataset_contract_error'}
SCORES = ('answer_correctness', 'answer_relevance', 'completeness', 'groundedness', 'format_compliance',
          'answer_quality_score', 'retrieval_quality_score', 'overall_score')


def summarize_eval(judges: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [_row(judge) if isinstance(judge, Mapping) else _invalid_row(index)
            for index, judge in enumerate(judges, 1)]
    quality_counts = Counter(row['quality_label'] for row in rows)
    failure_counts = Counter(row['failure_type'] for row in rows)
    retrieval_counts = Counter(row['retrieval_failure_type'] for row in rows)
    execution_failures = [
        _failure_row(row) for row in rows
        if row['failure_type'] in {'infra_failure', 'judge_contract_error'}
    ]
    routing_failures = [_failure_row(row) for row in rows if row['failure_type'] == 'dataset_contract_error']
    return {
        'id': 'eval.summary',
        'total': len(rows),
        'case_ids': [row['case_id'] for row in rows],
        'metrics': _metrics(rows),
        'quality_counts': dict(quality_counts),
        'failure_type_counts': dict(failure_counts),
        'retrieval_failure_type_counts': dict(retrieval_counts),
        'bad_cases': [row for row in rows if row['quality_label'] != 'good'],
        'routing_failures': routing_failures,
        'execution_failures': execution_failures,
        'checks': {'ready': not routing_failures and not execution_failures,
                   'errors': routing_failures + execution_failures},
        'rows': rows,
    }


def _row(judge: Mapping[str, Any]) -> dict[str, Any]:
    case = judge.get('case') if isinstance(judge.get('case'), Mapping) else {}
    answer = judge.get('rag_answer') if isinstance(judge.get('rag_answer'), Mapping) else {}
    target = judge.get('target') if isinstance(judge.get('target'), Mapping) else {}
    return {
        'case_id': str(judge.get('case_id') or case.get('id') or ''),
        'kb_id': str(target.get('kb_id') or ''),
        'question': str(case.get('question') or ''),
        'question_type': str(case.get('question_type') or ''),
        'ground_truth': case.get('answer'),
        'rag_answer': answer.get('answer'),
        **{key: judge.get(key, 0.0) for key in SCORES},
        'answer_score': judge.get('answer_quality_score', 0.0),
        'retrieval_score': judge.get('retrieval_quality_score', 0.0),
        'quality_label': str(judge.get('quality_label') or ''),
        'failure_type': str(judge.get('failure_type') or ''),
        'retrieval_failure_type': str(judge.get('retrieval_failure_type') or ''),
        'reason': str(judge.get('reason') or ''),
        'defect': str(judge.get('defect') or ''),
        'reference_chunk_ids': case.get('reference_chunk_ids') or [],
        'reference_doc_ids': case.get('reference_doc_ids') or [],
        'retrieve_chunk_ids': answer.get('chunk_ids') or [],
        'retrieve_doc_ids': answer.get('doc_ids') or [],
        'retrieve_contexts': answer.get('contexts') or [],
        'retrieved_contexts': answer.get('contexts') or [],
        'trace_id': str(judge.get('trace_id') or ''),
        'target': dict(target),
    }


def _metrics(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    scored = [
        row for row in rows
        if row.get('failure_type') not in UNSCORED and row.get('quality_label') != 'infra_failure'
    ]
    return {
        'scored_count': len(scored),
        'overall_score_avg': _avg(scored, 'overall_score'),
        'answer_quality_score_avg': _avg(scored, 'answer_quality_score'),
        'retrieval_quality_score_avg': _avg(
            [row for row in scored if row.get('retrieval_failure_type') != 'not_applicable'],
            'retrieval_quality_score',
        ),
        'answer_correctness_avg': _avg(scored, 'answer_correctness'),
        'groundedness_avg': _avg(scored, 'groundedness'),
        'answer_relevance_avg': _avg(scored, 'answer_relevance'),
        'correct_rate': _rate(scored, 'good'),
    }


def _avg(rows: list[Mapping[str, Any]], key: str) -> float:
    values = [float(row.get(key) or 0.0) for row in rows]
    return round(sum(values) / len(values), 4) if values else 0.0


def _rate(rows: list[Mapping[str, Any]], label: str) -> float:
    return round(sum(1 for row in rows if row.get('quality_label') == label) / len(rows), 4) if rows else 0.0


def _failure_row(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        'case_id': str(row.get('case_id') or ''),
        'kb_id': str(row.get('kb_id') or ''),
        'failure_type': str(row.get('failure_type') or ''),
        'reason': str(row.get('reason') or ''),
    }


def _invalid_row(index: int) -> dict[str, Any]:
    return {
        'case_id': f'invalid_{index:04d}',
        **{key: '' for key in ('kb_id', 'question', 'question_type', 'ground_truth', 'rag_answer')},
        **{key: 0.0 for key in ('answer_score', 'retrieval_score', *SCORES)},
        'quality_label': 'infra_failure',
        'failure_type': 'judge_contract_error',
        'retrieval_failure_type': 'not_applicable',
        'reason': 'judge result is not a mapping',
        'defect': 'judge_contract_error',
        'reference_chunk_ids': [],
        'reference_doc_ids': [],
        'retrieve_chunk_ids': [],
        'retrieve_doc_ids': [],
        'retrieve_contexts': [],
        'retrieved_contexts': [],
        'trace_id': '',
        'target': {},
    }
