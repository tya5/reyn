---
type: eval
app: dsl/apps/writing_review_app/app.md
judge_model: gpt-4o
---

## case: short_tech_article
input: "Pythonの非同期処理（asyncio）について、初心者向けの技術解説記事を書いてください。具体的なコード例を含めること。"

### phase: analyze
schema:
- key_points: string[]
- angle: string
- tone: string

quality:
- ターゲット読者（初心者）が正しく識別されていること。
- 記事で扱うべき主要トピック（asyncio の基本、使い方、コード例）が列挙されていること。

### phase: draft
schema:
- title: string
- body: string
- self_assessment: string

quality:
- asyncio の基本的な説明が含まれていること。
- 少なくとも1つのコード例が含まれていること。
- 初心者向けの言葉遣いになっていること。

### phase: review
schema:
- article.title: string
- article.body: string
- review_result.approved: boolean
- review_result.score: number, range 0.0-1.0
- review_result.feedback: string
- review_result.issues: string, min_length 1

quality:
- review_result.feedback に具体的な改善点または承認理由が記述されていること。
- review_result.issues に最低1つの改善点が記述されていること。

### phase: judge
schema:
- decision: string, equals "finish" or "revise"
- reason: string
- confidence: number, range 0.0-1.0
- article.title: string
- article.body: string
- revision_notes: string[], max 3

quality:
- decision が "finish" または "revise" のいずれかであること。
- decision が "finish" の場合、article.title と article.body が出力記事のタイトルと本文であること。
- decision が "revise" の場合、revision_notes に最大3つの具体的な修正指示が含まれていること。

### phase: revise
schema:
- title: string
- body: string
- self_assessment: string

quality:
- data.revision_notes に記載されたすべての修正指示が反映されていること。
- self_assessment が修正指示への対応状況を具体的に記述していること。

### cross_phase
- judge.article.title == revise.title
- judge.article.body == revise.body
- judge.article.title == final_article.title
- judge.article.body == final_article.body

### final
schema:
- title: string
- body: string
- quality_notes: string[]

quality:
- final_article.quality_notes にレビュー結果のサマリーが簡潔に記述されていること。
- opinion_piece のテストケースでは、最終記事の body が 800 文字以上であること。
- short_tech_article のテストケースでは、最終記事の body が 500 文字以上であること。
- 最終記事が指定された言語（例: 日本語）で書かれていること。

## case: opinion_piece
input: "AIが人間の仕事を奪うという議論について、バランスのとれた意見記事を書いてください。"

### phase: analyze
schema:
- key_points: string[]
- angle: string
- tone: string

quality:
- ターゲット読者（一般読者）が正しく識別されていること。
- 記事で扱うべき主要トピック（AIによる仕事への影響、肯定的な側面、否定的な側面、将来展望）が列挙されていること。

### phase: draft
schema:
- title: string
- body: string
- self_assessment: string

quality:
- AIが人間の仕事を奪うという議論について、バランスの取れた視点が含まれていること。
- 著者の立場が明確になっていること。
- 初心者にも理解しやすい言葉遣いになっていること。

### phase: review
schema:
- article.title: string
- article.body: string
- review_result.approved: boolean
- review_result.score: number, range 0.0-1.0
- review_result.feedback: string
- review_result.issues: string, min_length 1

quality:
- review_result.feedback に具体的な改善点または承認理由が記述されていること。
- review_result.issues に最低1つの改善点が記述されていること。

### phase: judge
schema:
- decision: string, equals "finish" or "revise"
- reason: string
- confidence: number, range 0.0-1.0
- article.title: string
- article.body: string
- revision_notes: string[], max 3

quality:
- decision が "finish" または "revise" のいずれかであること。
- decision が "finish" の場合、article.title と article.body が出力記事のタイトルと本文であること。
- decision が "revise" の場合、revision_notes に最大3つの具体的な修正指示が含まれていること。

### phase: revise
schema:
- title: string
- body: string
- self_assessment: string

quality:
- data.revision_notes に記載されたすべての修正指示が反映されていること。
- self_assessment が修正指示への対応状況を具体的に記述していること。

### cross_phase
- judge.article.title == revise.title
- judge.article.body == revise.body
- judge.article.title == final_article.title
- judge.article.body == final_article.body

### final
schema:
- title: string
- body: string
- quality_notes: string[]

quality:
- final_article.quality_notes にレビュー結果のサマリーが簡潔に記述されていること。
- opinion_piece のテストケースでは、最終記事の body が 800 文字以上であること。
- short_tech_article のテストケースでは、最終記事の body が 500 文字以上であること。
- 最終記事が指定された言語（例: 日本語）で書かれていること。
