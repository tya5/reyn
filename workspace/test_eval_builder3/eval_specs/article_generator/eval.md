---
type: eval
app: dsl/apps/article_generator/app.md
dsl_root: dsl/
judge_model: claude-3-opus-20240229
---

## case: Standard Article Generation
input: "AIの未来」について、専門家向けの解説記事を作成してください。

### phase: generate_draft
schema:
- content: string, min_length 100

quality:
- 生成されたドラフト記事は、ユーザーからの指示（例：「AIの未来」について、専門家向け）に沿っていること。
- 生成されたドラフト記事は、誤字脱字や不自然な日本語がないこと。

### phase: review_draft
schema:
- approved: boolean
- feedback: string, min_length 0

quality:
- レビュー結果は、ドラフト記事の内容に基づいて客観的に判断されていること。
- 修正が必要な場合（approved: false）、feedbackには具体的な改善点が記述されていること。

### final
schema:
- 

quality:
- 

## case: Article Requiring Revision
input: 「猫との暮らし」について、初心者向けのブログ記事を書いてください。ただし、専門用語は避けてください。

### phase: generate_draft
schema:
- content: string, min_length 100

quality:
- 生成されたドラフト記事は、ユーザーからの指示（例：「猫との暮らし」について、初心者向け、専門用語を避ける）に沿っていること。
- 生成されたドラフト記事は、誤字脱字や不自然な日本語がないこと。

### phase: review_draft
schema:
- approved: boolean
- feedback: string, min_length 0

quality:
- レビュー結果は、ドラフト記事の内容に基づいて客観的に判断されていること。
- 修正が必要な場合（approved: false）、feedbackには具体的な改善点が記述されていること。

### final
schema:
- 

quality:
- 
