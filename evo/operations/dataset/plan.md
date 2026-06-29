# Dataset Operation Plan

## Goal

`optimization_plan/operations/dataset` is responsible only for preparing an evaluation dataset.

It may:

- load one or more knowledge bases;
- import one or more existing evaluation CSV files;
- merge imported CSV cases and generated KB cases into `eval.case`;
- produce per-case `eval.case_preparation`;
- assemble all cases into `eval.dataset`;
- preserve provenance for CSV and KB sources.

It must not:

- call the target RAG service to answer questions;
- run evaluation or judging;
- analyze failures;
- repair or mutate application code.

## Inputs

The backend passes dataset sources as KB/CSV pairs.

Each source item contains:

- `kb_id`: required knowledge-base id.
- `csv_path`: optional evaluation CSV path for that KB.

Pure KB generation is allowed by passing a `kb_id` without `csv_path`.

Multiple source items are allowed. CSV rows are interpreted only in the context of the paired `kb_id`.

## CSV Contract

Canonical `eval.case` and `eval.dataset.cases` fields must align with the current evaluation CSV fields. Imported CSV cases and KB-generated cases use the same canonical field set.

Input CSV files must contain the current evaluation dataset fields. Column order may differ, but the required columns must be present.

Current dataset fields:

- `answer`
- `difficulty`
- `difficulty_rationale`
- `grading_guidance`
- `id`
- `question`
- `question_type`
- `reasoning_steps`
- `reference_chunk_ids`
- `reference_context`
- `reference_doc`
- `reference_doc_ids`
- `source_message_id`
- `source_preparation`
- `type_rationale`

Valid imported rows must have non-empty values for:

- `answer`
- `difficulty`
- `difficulty_rationale`
- `grading_guidance`
- `id`
- `question`
- `question_type`
- `reasoning_steps`
- `reference_chunk_ids`
- `reference_context`
- `reference_doc`
- `reference_doc_ids`

Optional fields:

- `source_message_id`
- `source_preparation`
- `type_rationale`

Rows with missing required columns, empty required values, invalid list values, or unsupported enum values are skipped and recorded in warnings. A bad row does not invalidate the whole CSV.

`question_type` must be one of the supported dataset question types. `difficulty` must be one of the supported difficulty values.

## Merge And Case Ids

All valid CSV rows are merged in backend-provided source order, then CSV row order.

Final case ids are reassigned by dataset operation:

- `case_0001`
- `case_0002`
- ...

The final `id` field in `eval.case` and downloaded CSV is the reassigned id.

The original CSV `id` is preserved as `original_id` in provenance/audit columns.

Imported rows keep their normalized evaluation fields, except `id` is replaced by the final case id.

If valid imported CSV rows are fewer than 100, dataset supplements from the paired KB sources until the dataset reaches 100 cases.

If valid imported CSV rows exceed 100, they are not truncated.

Pure KB generation does not require 100 cases. Its case count follows the runtime/requested partitions.

## Generated Cases

Generated cases use `source = generated_kb`.

Generated cases have:

- `original_id`: empty string;
- `kb_id`: the KB used to generate the case;
- final `id`: assigned as `case_NNNN`.

If a generated case ever uses evidence from multiple KBs, the implementation must keep KB identity explicit and must not collapse evidence by bare document id or bare chunk id.

Generation should first use the current simple generation path and only add small reliability improvements:

- select source evidence with deterministic rules by `question_type`;
- pass only selected evidence through `source_preparation_json`;
- let the LLM generate `question`, `answer`, `grading_guidance`, `reasoning_steps`, `difficulty_rationale`, and `type_rationale`;
- fill structural fields in code: `id`, `question_type`, `difficulty`, `reference_*`, `source_message_id`, and `source_preparation`;
- validate JSON shape and required generated fields;
- retry at most once on invalid LLM output.

The generated `answer` is the reference answer for the evaluation dataset. It is not a target RAG answer and must not call the target RAG service.

When imported CSV rows need supplementing and multiple KBs are available, generate supplemental cases by rotating through the available KBs in backend-provided order. Keep each generated case's `kb_id` explicit in provenance.

## Provenance And Download Columns

`eval.dataset` should include provenance suitable for API display and dataset download.

Downloaded CSV should keep the normal evaluation fields and append audit columns:

- `original_id`
- `source`
- `kb_id`

`source` values:

- `imported_csv`
- `generated_kb`

The final CSV `id` remains the canonical dataset case id.

Minimal per-case provenance:

- `final_id`
- `original_id`
- `source`
- `kb_id`

Other evidence fields come from the normalized evaluation CSV fields and generated case fields.

Audit columns are for dataset download and API display. They do not replace the canonical case fields consumed by later eval and analysis stages.

## Knowledge-Base Evidence Checks

Evidence checks must use the paired `kb_id`.

For imported CSV rows, the dataset operation should check the corresponding knowledge base directly, not only the built snapshot, to avoid missing valid evidence.

Minimum checks:

- `kb_id + chunk_id` exists in the paired KB;
- `kb_id + doc_id` or `kb_id + doc_ref` exists in the paired KB when document ids are available.

The operation must not treat bare `chunk_id` or bare `doc_id` as globally unique.

Different KBs may contain the same document id or chunk id. Identity must remain scoped by `kb_id`.

`reference_doc_ids` should be minimally deduplicated, but deduplication must include `kb_id` in the identity key.

The plan does not require `reference_context`, `reference_doc`, and `reference_chunk_ids` to have equal lengths as an import-validity rule. The implementation should only enforce length constraints that are needed for a concrete evidence mapping and should otherwise preserve the CSV fields.

Rows whose evidence cannot be verified may be kept or skipped according to the final implementation policy, but the result must be explicit in warnings and provenance. The preferred behavior is to keep structurally valid rows and mark evidence verification failure as a warning.

## Warnings

Warnings should be simple enough for API display and tests.

Minimal warning fields:

- `code`
- `message`
- `case_id`
- `original_id`
- `kb_id`

Suggested warning codes:

- `csv_row_invalid`
- `csv_evidence_unverified`
- `csv_supplemented`
- `dataset_failed`

Warnings should be included in dataset-stage artifacts so the API can return them for display.

## Failure Rules

Invalid CSV rows are skipped with warnings.

If all CSV rows are invalid but KB sources are available, dataset generation continues from KB and follows the CSV scenario supplement target of 100 cases.

If CSV rows are insufficient and KB sources are unavailable or empty, the dataset operation fails with a clear error.

If a pure KB request has no usable KB evidence, the dataset operation fails with a clear error.

## Outputs

Dataset operation outputs:

- `corpus.report`
- `corpus.snapshot`
- partitioned `eval.case_preparation`
- partitioned `eval.case`
- root `eval.dataset`

`eval.dataset` should include:

- normalized cases;
- final case ids;
- question type and difficulty stats;
- readiness checks;
- warnings;
- per-case provenance/audit mapping.

## Verification Criteria

Implementation should be verified with contract tests for:

- multiple KB/CSV source pairs;
- CSV column order independence;
- invalid CSV rows skipped and recorded;
- final ids reassigned as `case_0001...`;
- `original_id/source/kb_id` preserved in dataset provenance and downloadable CSV shape;
- imported CSV fewer than 100 supplemented from KB;
- imported CSV greater than 100 not truncated;
- pure KB generation not forced to 100;
- duplicate bare document ids across KBs not merged;
- duplicate bare chunk ids across KBs not treated as the same evidence;
- dataset stage not producing answers, judge results, analysis results, or repair artifacts.
