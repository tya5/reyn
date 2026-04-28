---
type: eval
app: dsl/apps/eval_builder/app.md
dsl_root: dsl/
judge_model: gpt-4o
---

## case: eval_builder_self
input: "dsl/apps/eval_builder/app.md の eval.md を作って"

### phase: analyze_app
schema:
- app_dsl_path: string, min_length 1
- dsl_root: string, min_length 1
- app_name: string, min_length 1
- phase_order: array, min 1
- test_cases: array, min 1
- phase_eval_designs: array, min 1
- final_schema: array
- final_quality: array

quality:
- app_dsl_path が "dsl/apps/eval_builder/app.md" を指している
- app_name が "eval_builder" である
- phase_order に "analyze_app" と "write_eval" の両方が含まれている
- test_cases の各項目に name, input, rationale が含まれている
- phase_eval_designs に analyze_app と write_eval の両フェーズが含まれている
- [aspirational] phase_eval_designs の schema/quality が具体的な artifact フィールド名を参照している

### phase: write_eval
schema:
- eval_md_path: string, min_length 1
- case_count: integer, min 1
- total_criteria: integer, min 4
- next_steps: string, min_length 1

quality:
- eval_md_path が "eval_specs/eval_builder/eval.md" の形式である
- 生成された eval.md に schema: セクションが含まれている

### cross_phase
- analyze_app.app_dsl_path == write_eval.app_dsl_path

### final
schema:
- eval_md_path: string, min_length 1
- case_count: integer, min 1
- total_criteria: integer, min 4
- next_steps: string, min_length 1

quality:
- next_steps にファイルの場所と実行方法が含まれている

## case: writing_review_app_eval
input: "dsl/apps/writing_review_app/app.md の eval.md を作って"

### phase: analyze_app
schema:
- app_dsl_path: string, min_length 1
- app_name: string, min_length 1
- phase_order: array, min 2
- test_cases: array, min 1
- phase_eval_designs: array, min 2
- final_schema: array
- final_quality: array

quality:
- app_dsl_path が "dsl/apps/writing_review_app/app.md" を指している
- app_name が "writing_review_app" である
- phase_order に writing_review_app の実際のフェーズ（analyze, draft, review 等）が含まれている
- [aspirational] phase_eval_designs の schema が analysis_result や draft_article など実際の artifact フィールドを参照している

### phase: write_eval
schema:
- eval_md_path: string, min_length 1
- case_count: integer, min 1
- total_criteria: integer, min 4
- next_steps: string, min_length 1

quality:
- eval_md_path が "eval_specs/writing_review_app/eval.md" の形式である
- next_steps に eval の実行コマンドが含まれている

### final
schema:
- eval_md_path: string, min_length 1
- case_count: integer, min 1
- total_criteria: integer, min 4
- next_steps: string, min_length 1

quality:
- next_steps にファイルの場所と実行方法が含まれている
