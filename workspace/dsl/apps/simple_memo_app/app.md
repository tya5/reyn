---
type: app
name: simple_memo_app
entry: create_memo
final_output: finished_memo_app
final_output_description: 完成したメモ帳アプリ。
finish_criteria:
  - メモの作成・保存・読み込み機能が実装されていること。
  - アプリがユーザーの要望を満たしていること。
---

create_memo -> design_memo_storage
design_memo_storage -> implement_memo_app
implement_memo_app -> review_memo_app
review_memo_app -> implement_memo_app
