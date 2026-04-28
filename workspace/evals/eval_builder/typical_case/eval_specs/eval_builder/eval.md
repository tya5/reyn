---
type: eval
app: dsl/apps/eval_builder/app.md
judge_model: claude-3-opus-20240229
---

## case: 標準的なアプリDSLの分析
input: "dsl/apps/eval_builder/app.md"

### phase: analyze_app
- app_dsl_path フィールドが文字列型で存在すること。
- dsl_root フィールドが文字列型で存在すること。
- app_name フィールドが文字列型で存在すること。
- phase_order フィールドが文字列型の配列で存在し、少なくとも 'analyze_app' と 'write_eval' を含んでいること。
- test_cases フィールドが、name, input, rationale を持つオブジェクトの配列で存在すること。
- phase_eval_designs フィールドが、phase, artifact_type, criteria を持つオブジェクトの配列で存在すること。
- final_criteria フィールドが文字列の配列で存在すること。

### final
- 最終出力アーティファクト（eval_spec_result）が、eval_md_path, summary, num_test_cases, num_criteria, next_steps を正確に含んでいること。
- eval_md_path フィールドに指定されたパスに eval.md ファイルが正しく出力されていること。
- summary フィールドは、生成された eval.md の内容の要約として適切であること。
- next_steps フィールドは、ユーザーが eval.md を見つけ、利用するための明確な指示を含んでいること。

## case: レビューフェーズを含むアプリDSLの分析
input: "dsl/apps/review_app/app.md"

### phase: analyze_app
- app_dsl_path フィールドが文字列型で存在すること。
- dsl_root フィールドが文字列型で存在すること。
- app_name フィールドが文字列型で存在すること。
- phase_order フィールドが文字列型の配列で存在し、少なくとも 'analyze_app' と 'write_eval' を含んでいること。
- test_cases フィールドが、name, input, rationale を持つオブジェクトの配列で存在すること。
- phase_eval_designs フィールドが、phase, artifact_type, criteria を持つオブジェクトの配列で存在すること。
- final_criteria フィールドが文字列の配列で存在すること。

### final
- 最終出力アーティファクト（eval_spec_result）が、eval_md_path, summary, num_test_cases, num_criteria, next_steps を正確に含んでいること。
- eval_md_path フィールドに指定されたパスに eval.md ファイルが正しく出力されていること。
- summary フィールドは、生成された eval.md の内容の要約として適切であること。
- next_steps フィールドは、ユーザーが eval.md を見つけ、利用するための明確な指示を含んでいること。
