---
type: eval
app: dsl/apps/eval_builder/app.md
judge_model: gpt-4o
---

## case: typical_eval_request
input: "dsl/apps/writing_review_app/app.md の eval.md を作って"

### phase: analyze_app
schema:
- app_dsl_path: string, min_length 1
- dsl_root: string, min_length 1
- app_name: string, min_length 1
- judge_model: string, min_length 1
- phase_order: array, min 1
- test_cases: array, min 1
- phase_eval_designs: array, min 1
- cross_phase_assertions: array
- final_schema: array
- final_quality: array

quality:
- app_dsl_pathがユーザーメッセージから正しく抽出されていること。
- dsl_rootがapp_dsl_pathから正しく推測されていること。
- app_nameがapp.mdから正しく取得されていること。
- judge_modelが提案されていること。
- phase_orderにanalyze_appとwrite_evalが含まれていること。

### phase: write_eval
schema:
- eval_md_path: string, min_length 1
- app_dsl_path: string, min_length 1
- case_count: integer, min 1
- total_criteria: integer, min 1
- next_steps: string, min_length 1

quality:
- eval.mdの内容が指定されたフォーマットに厳密に従っていること。
- next_stepsにユーザーがeval.mdを実行するための適切な指示が含まれていること。

### final
schema:
- eval_md_path: string, min_length 1
- app_dsl_path: string, min_length 1
- case_count: integer, min 1
- total_criteria: integer, min 1
- next_steps: string, min_length 1

quality:
- eval_md_pathがworkspace内のパスを示していること。
- next_stepsの指示が明確で、ユーザーが次のアクションを実行できること。
