---
type: eval
app: dsl/apps/app_builder/app.md
dsl_root: dsl/
judge_model: gpt-4o
---

## case: simple_review_app
input: "レビューアプリを作ってください。ユーザーが文章を提出し、レビュアーが承認または差し戻しできるアプリです。差し戻しの場合は理由を記載します。"

### phase: plan_app
schema:
- app_name: string, min_length 1
- app_path: string, min_length 1
- entry_phase: string, min_length 1
- phases: array, min 2
- transitions: array, min 1
- artifacts: array, min 1

quality:
- app_name が snake_case 形式の文字列である
- app_path が "dsl/apps/{app_name}" の形式である
- entry_phase が phases 配列のいずれかの name と一致する
- final_output オブジェクトが存在し、name と fields を持つ
- フェーズの設計がユーザー要件（提出 → レビュー → 承認/差し戻し）を反映している
- [aspirational] phases 内の各フェーズに name, role, input_artifact, instructions が含まれている

### phase: build_app
schema:
- app_name: string, min_length 1
- app_path: string, min_length 1
- files_written: array, min 3, contains "app.md"
- file_count: integer, min 3
- summary: string, min_length 1

quality:
- file_count が files_written の要素数と一致している
- summary がアプリの目的（提出・レビュー・承認/差し戻し）を説明している

### cross_phase
- plan_app.app_name == build_app.app_name

### final
schema:
- app_name: string, min_length 1
- app_path: string, min_length 1
- files_written: array, min 3, contains "app.md"
- file_count: integer, min 1
- summary: string, min_length 1

quality:
- summary がアプリの目的を説明している

## case: feedback_analysis_app
input: "ユーザーフィードバックを収集し、改善提案を行うアプリを作ってください。フィードバック収集フェーズと分析・提案フェーズの 2 フェーズ構成にしてください。"

### phase: plan_app
schema:
- app_name: string, min_length 1
- app_path: string, min_length 1
- entry_phase: string, min_length 1
- phases: array, min 2
- transitions: array, min 1
- artifacts: array, min 1

quality:
- app_name が snake_case 形式の文字列である
- entry_phase が phases 配列のいずれかの name と一致する
- final_output オブジェクトが存在し、name と description と fields を持つ
- フェーズの設計が 2 フェーズ構成（収集 → 分析）という要件を満たしている
- [aspirational] phases 内の各フェーズに name, role, input_artifact, instructions が含まれている

### phase: build_app
schema:
- app_name: string, min_length 1
- files_written: array, min 3, contains "app.md"
- file_count: integer, min 3
- summary: string, min_length 1

quality:
- file_count が files_written の要素数と一致している
- summary がフィードバック収集と改善提案という 2 つの機能に言及している

### cross_phase
- plan_app.app_name == build_app.app_name

### final
schema:
- app_name: string, min_length 1
- app_path: string, min_length 1
- files_written: array, min 3, contains "app.md"
- file_count: integer, min 1
- summary: string, min_length 1

quality:
- summary がアプリの目的を説明している
