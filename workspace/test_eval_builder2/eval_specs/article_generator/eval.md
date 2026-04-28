---
type: eval
app: dsl/apps/article_generator/app.md
dsl_root: dsl/
judge_model: claude-3-haiku-20240307
---

## case: Typical article generation
input: "AIの未来について、技術的な側面と社会的な影響の両方を含んだ記事を作成してください。ターゲット読者は一般読者です。"

### phase: generate_article
schema:
- content: string, min_length 100

quality:
- 生成された記事のコンテンツは、ユーザーの指示（「AIの未来」）に沿っており、技術的な側面と社会的な影響の両方を含んでいる。
- 記事は明確で、構造化されており、情報が正確である。

### phase: review_article
schema:
- approved: boolean
- feedback: string, min_length 10

quality:
- レビューは、明確さ、構造、正確さ、文法、スタイルに関する具体的なフィードバックを提供している（approved: false の場合）。
- approved が true の場合、feedback フィールドは空でもよい。

## case: Article needing revision
input: "再生可能エネルギーの現状について、技術的な詳細に焦点を当てた記事を作成してください。ただし、複雑すぎる専門用語は避けてください。"

### phase: generate_article
schema:
- content: string, min_length 100

quality:
- 生成された記事のコンテンツは、ユーザーの指示（「再生可能エネルギーの現状」）に沿っており、技術的な詳細に焦点を当てている。
- 記事は明確で、構造化されており、情報が正確である。
- 複雑すぎる専門用語は避けられている。

### phase: review_article
schema:
- approved: boolean
- feedback: string, min_length 10

quality:
- レビューは、明確さ、構造、正確さ、文法、スタイルに関する具体的なフィードバックを提供している（approved: false の場合）。
- approved が true の場合、feedback フィールドは空でもよい。
