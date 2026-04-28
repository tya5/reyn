---
type: phase
name: review_explanation
input: explanation_article
input_description: 生成されたアーキテクチャ解説記事。
role: explanation_reviewer
can_finish: false
---

作成された解説記事をレビューし、以下の基準に基づいて品質を評価してください。1. アーキテクチャの正確性、2. 各コンポーネントの詳細度、3. 分かりやすさと網羅性。問題がある場合は、具体的なフィードバックを記載し、リバイスが必要と判断してください。承認する場合は、approved: true と設定してください。
