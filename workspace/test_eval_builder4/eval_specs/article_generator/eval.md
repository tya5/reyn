---
type: eval
app: dsl/apps/article_generator/app.md
dsl_root: dsl/
judge_model: claude-3-haiku-20240307
---

## case: Typical Article Generation
input: "{"text": "「AIの未来」というテーマで、テクノロジーに関心のある一般読者向けに、簡潔で分かりやすい記事を作成してください。キーワードとして「機械学習」「ディープラーニング」「シンギュラリティ」を含めてください。"}"

### phase: generate_article
schema:
- title: string
- content: string
- content: min_length 100

quality:
- 生成された記事のタイトルは、ユーザーの指示（トピック、読者層）を反映していること。
- 生成された記事の本文は、ユーザーの指示（トピック、読者層、キーワード）に沿って、簡潔かつ分かりやすく記述されていること。

### phase: review_article
schema:
- approved: boolean
- feedback: string, min_length 0, max_length 500

quality:
- レビュー結果（approved: true/false）は、レビュー基準（正確性、明瞭性、構成、指示準拠）に基づいて論理的に判断されていること。
- 差し戻し（approved: false）の場合、feedbackフィールドには具体的な修正点が記述されていること。

### final
schema:
- final_article.title: string
- final_article.content: string
- final_article.title: min_length 5
- final_article.content: min_length 200

quality:
- 最終的な記事は、ユーザーの当初の指示（トピック、読者層、キーワードなど）をすべて満たしており、質が高いこと。

## case: Article Needing Revision
input: "{"text": "「日本の伝統工芸」について、専門家向けに詳細な記事を書いてください。ただし、専門用語は避け、平易な言葉で説明してください。また、歴史的背景も詳しく触れてください。"}"

### phase: generate_article
schema:
- title: string
- content: string
- content: min_length 100

quality:
- 生成された記事のタイトルは、ユーザーの指示（トピック、読者層）を反映していること。
- 生成された記事の本文は、ユーザーの指示（トピック、読者層、キーワード）に沿って、簡潔かつ分かりやすく記述されていること。

### phase: review_article
schema:
- approved: boolean
- feedback: string, min_length 0, max_length 500

quality:
- レビュー結果（approved: true/false）は、レビュー基準（正確性、明瞭性、構成、指示準拠）に基づいて論理的に判断されていること。
- 差し戻し（approved: false）の場合、feedbackフィールドには具体的な修正点が記述されていること。

### final
schema:
- final_article.title: string
- final_article.content: string
- final_article.title: min_length 5
- final_article.content: min_length 200

quality:
- 最終的な記事は、ユーザーの当初の指示（トピック、読者層、キーワードなど）をすべて満たしており、質が高いこと。
