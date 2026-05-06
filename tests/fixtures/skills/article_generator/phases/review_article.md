---
type: phase
name: review_article
input: generated_article
input_description: 生成された記事の下書き。
role: reviewer
can_finish: true
---

生成された記事の下書きをレビューしてください。以下の基準を満たしているか確認し、結果を承認（approved: true）または差し戻し（approved: false）で返してください。

レビュー基準:
- 内容の正確性: 事実に基づいているか。
- 明瞭性: 分かりやすく書かれているか。
- 構成: 論理的な流れになっているか。
- 指示への準拠: ユーザーの指示（トピック、読者層など）に従っているか。

フィードバック（差し戻しの場合）: 修正が必要な点を具体的に記述してください。