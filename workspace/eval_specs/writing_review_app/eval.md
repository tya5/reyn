---
type: eval
app: dsl/apps/writing_review_app/app.md
dsl_root: dsl/
judge_model: gpt-4o
---

## case: short_tech_article
input: "Pythonの非同期処理（asyncio）について、初心者向けの技術解説記事を書いてください。具体的なコード例を含めること。"

### phase: analyze
schema:
- key_points: array, min 3
- angle: string, min_length 1
- tone: string, min_length 1

quality:
- angle フィールドが記事の方向性や切り口を明確に説明している
- key_points の各項目が asyncio に関連した具体的なトピックである

### phase: draft
schema:
- title: string, min_length 1
- body: string, min_length 100
- self_assessment: string, min_length 1

quality:
- body に asyncio の基本概念の説明が含まれている
- body に少なくとも1つの Python コード例が含まれている

### phase: review
schema:
- review_result.score: number, range 0.0-1.0
- review_result.strengths: array, min 1
- review_result.issues: array, min 1

quality:
- review_result.strengths の各項目が記事の具体的な強みを説明している
- [aspirational] review_result.issues の各項目が改善すべき具体的な問題を指摘している

### phase: revise
schema:
- title: string, min_length 1
- body: string, min_length 100
- self_assessment: string, min_length 1

quality:
- [aspirational] revise 後の body が draft より改善されている

### final
schema:
- title: string, min_length 1
- body: string, min_length 100
- quality_notes: array, min 1

quality:
- 最終記事は日本語で書かれている
- body に asyncio の技術的内容が含まれている

## case: opinion_piece
input: "AIが人間の仕事を奪うという議論について、バランスのとれた意見記事を書いてください。"

### phase: analyze
schema:
- key_points: array, min 3
- angle: string, min_length 1
- tone: string, min_length 1

quality:
- angle フィールドがバランスのとれた意見記事としての方向性を説明している
- key_points にAIと仕事に関する複数の視点が含まれている

### phase: draft
schema:
- title: string, min_length 1
- body: string, min_length 100
- self_assessment: string, min_length 1

quality:
- body にAIと仕事に関する複数の視点（賛否両論）が含まれている
- body に著者の結論または立場が明示されている

### phase: review
schema:
- review_result.score: number, range 0.0-1.0
- review_result.strengths: array, min 1
- review_result.issues: array, min 1

quality:
- review_result.strengths の各項目が記事の具体的な強みを説明している
- [aspirational] review_result.issues の各項目が改善すべき具体的な問題を指摘している

### phase: revise
schema:
- title: string, min_length 1
- body: string, min_length 100
- self_assessment: string, min_length 1

quality:
- revise 後の body にAIと仕事の議論に関する複数の視点が維持されている

### final
schema:
- title: string, min_length 1
- body: string, min_length 100
- quality_notes: array, min 1

quality:
- 最終記事は日本語で書かれている
- body にAIと仕事の関係に関する議論が含まれている
