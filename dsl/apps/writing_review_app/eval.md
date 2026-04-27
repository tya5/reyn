---
type: eval
app: dsl/apps/writing_review_app/app.md
dsl_root: dsl/
judge_model: gpt-4o
---

## case: short_tech_article
input: "Pythonの非同期処理（asyncio）について、初心者向けの技術解説記事を書いてください。具体的なコード例を含めること。"

### phase: analyze
- ユーザーのリクエストからターゲット読者（初心者）が正しく識別されている
- 記事で扱うべき主要トピック（asyncio の基本、使い方、コード例）が列挙されている

### phase: draft
- 記事にタイトルが含まれている
- asyncio の基本的な説明がある
- 少なくとも1つのコード例が含まれている
- 初心者向けの言葉遣いになっている

### phase: review
- approved フィールドが boolean 型で存在する
- feedback フィールドに具体的な改善点または承認理由がある

### final
- 最終記事に title フィールドがある
- 最終記事の body が 500 文字以上ある
- quality_notes にレビュー結果のサマリーがある

## case: opinion_piece
input: "AIが人間の仕事を奪うという議論について、バランスのとれた意見記事を書いてください。"

### phase: analyze
- AI と雇用に関する主要な論点が特定されている
- 記事の構成（賛成意見・反対意見・結論）が計画されている

### phase: draft
- 賛成側と反対側の両方の視点が含まれている
- 著者の立場または結論が明示されている

### final
- 最終記事に title フィールドがある
- 最終記事の body が 800 文字以上ある
- 日本語で書かれている
