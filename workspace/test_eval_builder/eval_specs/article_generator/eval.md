---
type: eval
app: dsl/apps/article_generator/app.md
dsl_root: dsl/
judge_model: gpt-4o-mini
---

## case: 標準的な記事生成
input: "「AIの未来」についてのブログ記事を作成してください。ターゲット読者は一般読者で、専門用語は避けてください。"

### phase: generate_article
schema:
- content: string, min_length 100

quality:
- 生成された記事は、指示されたトピック（例：「AIの未来」）を正確にカバーしていること。
- 記事は明確で、構造化されており、読者が理解しやすいこと。

### phase: review_article
schema:
- approved: boolean
- feedback: string

quality:
- レビュー結果は、記事の明確さ、構造、正確さ、文法、スタイルに関する具体的なフィードバックを含んでいること（approved: false の場合）。
- 承認された記事（approved: true）は、指示された要件を満たしていること。

### final
schema:
- content: string, min_length 100

quality:
- 最終的な記事は、ユーザーの要求とレビューのフィードバック（もしあれば）をすべて満たしていること。

## case: 修正が必要な記事生成
input: "「量子コンピューティングの入門」についての記事を作成してください。ただし、専門用語を多用し、深すぎる技術的解説を含めてください。"

### phase: generate_article
schema:
- content: string, min_length 100

quality:
- 生成された記事は、指示されたトピック（例：「AIの未来」）を正確にカバーしていること。
- 記事は明確で、構造化されており、読者が理解しやすいこと。

### phase: review_article
schema:
- approved: boolean
- feedback: string

quality:
- レビュー結果は、記事の明確さ、構造、正確さ、文法、スタイルに関する具体的なフィードバックを含んでいること（approved: false の場合）。
- 承認された記事（approved: true）は、指示された要件を満たしていること。

### final
schema:
- content: string, min_length 100

quality:
- 最終的な記事は、ユーザーの要求とレビューのフィードバック（もしあれば）をすべて満たしていること。
