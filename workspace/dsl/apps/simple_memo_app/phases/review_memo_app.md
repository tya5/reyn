---
type: phase
name: review_memo_app
input: memo_app_code
input_description: 実装されたメモ帳アプリのコード。
role: reviewer
model_class: standard
can_finish: true
---

実装されたメモ帳アプリのコードをレビューします。以下の基準で評価してください：1. 要求された機能（作成、保存、読み返し）がすべて実装されているか。2. コードはクリーンで保守可能か。3. エラーハンドリングは適切か。レビュー結果を`approved`（boolean）、`feedback`（string）フィールドに記録し、`approved`がtrueであればこのフェーズで終了、falseであれば`implement_memo_app`にフィードバックを渡して修正を依頼してください。
