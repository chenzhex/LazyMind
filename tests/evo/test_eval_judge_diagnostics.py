import json
import sys
import types


fake_router = types.ModuleType('evo.operations.chat_router')


class RouterChatRequest:
    pass


fake_router.RouterChatRequest = RouterChatRequest
fake_router.call_router_chat = lambda request: {}
sys.modules.setdefault('evo.operations.chat_router', fake_router)

fake_repair = types.ModuleType('json_repair')
fake_repair.repair_json = lambda raw, return_objects=False: json.loads(raw) if return_objects else raw
sys.modules.setdefault('json_repair', fake_repair)

from evo.operations.eval.judge import judge_case
from evo.operations.eval.materializers import build_eval_detail_summary, eval_materializers


class FakeLLM:
    def __init__(self, *, llm_config=None, model=None):
        self.llm_config = llm_config
        self.model = model

    def __call__(self, prompt, **kwargs):
        return json.dumps({
            'answer_correctness': 0.9,
            'answer_relevance': 1.0,
            'completeness': 0.8,
            'groundedness': 0.8,
            'format_compliance': 1.0,
            'failure_type': 'none',
            'reason': 'legacy judge output without diagnostic fields',
            'defect': '',
        })


def test_judge_adds_key_point_and_rank_diagnostics(monkeypatch):
    import evo.llm

    monkeypatch.setattr(evo.llm, 'LazyLLMClient', FakeLLM)
    case = {
        'id': 'case_0001',
        'question': 'When is launch and what is the price?',
        'answer': 'The launch date is July 8. The price is 20 USD.',
        'question_type': 'single_hop',
        'difficulty': 'medium',
        'key_points': [
            {'id': 'date', 'statement': 'The launch date is July 8.', 'weight': 1.0, 'required': True},
            {'id': 'price', 'statement': 'The price is 20 USD.', 'weight': 1.0, 'required': True},
        ],
        'reference_chunk_ids': ['chunk-a'],
        'reference_doc_ids': ['doc-a'],
        'reference_context': [
            'The launch date is July 8. The price is 20 USD.',
        ],
    }
    answer = {
        'status': 'ok',
        'case_id': 'case_0001',
        'answer': 'The launch date is July 8.',
        'contexts': [
            {'chunk_id': 'chunk-a', 'doc_id': 'doc-a', 'content': 'The launch date is July 8.', 'rank': 1},
            {'chunk_id': 'chunk-noise', 'doc_id': 'doc-noise', 'content': 'Unrelated content.', 'rank': 2},
        ],
        'chunk_ids': ['chunk-a', 'chunk-noise'],
        'doc_ids': ['doc-a', 'doc-noise'],
        'trace_id': 'trace-1',
        'target': {'algorithm_id': 'algo-a', 'kb_id': 'kb-a'},
    }

    result = judge_case(case, answer, {'judge_llm_config': {'evo_llm': {'model': 'fake'}}})

    assert result['key_point_recall'] == 0.5
    assert result['matched_key_points'][0]['id'] == 'date'
    assert result['missing_points'][0]['id'] == 'price'
    assert result['retrieval_hit_at_k'] == 1.0
    assert result['retrieval_mrr'] == 1.0
    assert result['retrieval_precision_at_k'] == 0.5
    assert result['answer_quality_score'] < 0.9
    assert result['quality_label'] == 'partial'
    assert result['metric_layers']['primary_scores'] == [
        'overall_score',
        'answer_quality_score',
        'retrieval_quality_score',
        'quality_label',
        'failure_type',
        'retrieval_failure_type',
    ]
    assert result['score_breakdown']['answer_quality_score']['weights']['key_point_recall'] == 0.20


def test_detail_summary_exposes_diagnostics(monkeypatch):
    import evo.llm

    monkeypatch.setattr(evo.llm, 'LazyLLMClient', FakeLLM)
    case = {
        'id': 'case_0002',
        'question': 'What is the supported fact?',
        'answer': 'The supported fact is alpha.',
        'question_type': 'single_hop',
        'difficulty': 'easy',
        'key_points': ['The supported fact is alpha.'],
        'reference_chunk_ids': ['chunk-a'],
        'reference_context': ['The supported fact is alpha.'],
    }
    answer = {
        'status': 'ok',
        'case_id': 'case_0002',
        'answer': 'The supported fact is alpha.',
        'contexts': ['The supported fact is alpha.'],
        'chunk_ids': ['chunk-a'],
        'doc_ids': [],
        'trace_id': 'trace-2',
        'target': {'algorithm_id': 'algo-a', 'kb_id': 'kb-a'},
    }
    judge = judge_case(case, answer, {'judge_llm_config': {'evo_llm': {'model': 'fake'}}})

    summary = build_eval_detail_summary([judge])

    assert summary['metrics']['key_point_recall_avg'] == 1.0
    assert summary['by_question_type']['single_hop']['count'] == 1
    assert summary['by_difficulty']['easy']['correct_rate'] == 1.0
    assert summary['rows'][0]['matched_key_points']
    assert summary['rows'][0]['metric_layers']['core_explainers']
    assert summary['rows'][0]['score_breakdown']['overall_score']['weights']
    assert 'retrieval_mrr_avg' in summary['metrics']


def test_legacy_case_without_key_points_keeps_existing_label_logic(monkeypatch):
    import evo.llm

    monkeypatch.setattr(evo.llm, 'LazyLLMClient', FakeLLM)
    case = {
        'id': 'case_0003',
        'question': 'What is the supported fact?',
        'answer': 'The supported fact is alpha.',
        'question_type': 'single_hop',
        'difficulty': 'easy',
        'reference_chunk_ids': ['chunk-a'],
        'reference_context': ['The supported fact is alpha.'],
    }
    answer = {
        'status': 'ok',
        'case_id': 'case_0003',
        'answer': 'The supported fact is alpha.',
        'contexts': ['The supported fact is alpha.'],
        'chunk_ids': ['chunk-a'],
        'doc_ids': [],
        'trace_id': 'trace-3',
        'target': {'algorithm_id': 'algo-a', 'kb_id': 'kb-a'},
    }

    result = judge_case(case, answer, {'judge_llm_config': {'evo_llm': {'model': 'fake'}}})

    assert result['key_point_recall'] == 0.9
    assert result['quality_label'] == 'good'
    assert result['failure_type'] == 'none'


def test_eval_judge_materializer_passes_diagnostic_fields_to_downstream(monkeypatch):
    import evo.llm

    monkeypatch.setattr(evo.llm, 'LazyLLMClient', FakeLLM)
    case = {
        'id': 'case_0004',
        'question': 'When is launch and what is the price?',
        'answer': 'The launch date is July 8. The price is 20 USD.',
        'question_type': 'single_hop',
        'difficulty': 'medium',
        'key_points': [
            {'id': 'date', 'statement': 'The launch date is July 8.', 'weight': 1.0, 'required': True},
            {'id': 'price', 'statement': 'The price is 20 USD.', 'weight': 1.0, 'required': True},
        ],
        'reference_chunk_ids': ['chunk-a'],
        'reference_doc_ids': ['doc-a'],
        'reference_context': ['The launch date is July 8. The price is 20 USD.'],
    }
    answer = {
        'status': 'ok',
        'case_id': 'case_0004',
        'answer': 'The launch date is July 8.',
        'contexts': [
            {'chunk_id': 'chunk-a', 'doc_id': 'doc-a', 'content': 'The launch date is July 8.', 'rank': 1},
            {'chunk_id': 'chunk-noise', 'doc_id': 'doc-noise', 'content': 'Unrelated content.', 'rank': 2},
        ],
        'chunk_ids': ['chunk-a', 'chunk-noise'],
        'doc_ids': ['doc-a', 'doc-noise'],
        'trace_id': 'trace-4',
        'target': {'algorithm_id': 'algo-a', 'kb_id': 'kb-a'},
    }
    materializer = eval_materializers()['eval.judge']

    output = materializer(None, {
        'case': case,
        'answer': answer,
        'policy': {
            'judge_llm_config': {'evo_llm': {'model': 'fake'}},
            'key_point_recall_floor': 0.8,
            'retrieval_top_k': 5,
        },
    })
    judge = output['judge']

    for key in (
        'key_point_recall',
        'key_point_precision',
        'matched_key_points',
        'missing_points',
        'claim_support_rate',
        'unsupported_claims',
        'retrieval_hit_at_k',
        'retrieval_recall_at_k',
        'retrieval_precision_at_k',
        'retrieval_mrr',
        'retrieval_ndcg',
        'context_noise_rate',
        'numeric_accuracy',
        'list_set_f1',
        'semantic_similarity',
        'metric_layers',
        'score_breakdown',
    ):
        assert key in judge
    assert judge['missing_points'][0]['id'] == 'price'
    assert judge['eval_policy']['key_point_recall_floor'] == 0.8
    assert judge['eval_policy']['retrieval_top_k'] == 5
    assert 'retrieval_mrr' in judge['metric_layers']['core_explainers']
    assert judge['score_breakdown']['retrieval_quality_score']['weights']['retrieval_mrr'] == 0.20
