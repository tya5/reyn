---
type: reference
topic: stdlib
audience: [human, agent]
---

# Stdlib Skill

Reyn に付属するバンドルされた Skill。名前のルックアップでは最後に解決されます（`reyn/project/` と `reyn/local/` の後）。

| Skill | 目的 |
|-------|---------|
| [skill_builder](skill_builder.md) | 自然言語の説明から新しい Skill を生成する |
| [skill_improver](skill_improver.md) | eval スペックに対して Skill を反復的に改善する |
| [skill_importer](skill_importer.md) | 外部 Skill（例: Claude skill）を Reyn にインポートする |
| [eval](eval.md) | LLM-as-judge を使って 1 つのテストケースを評価する |
| [eval_builder](eval_builder.md) | Skill の eval スペック（`eval.md`）を生成する |
| [skill_router](skill_router.md) | chat の発話を Skill、ピア agent、または直接返信にルーティングする（`reyn chat` が使用）。Memory をインラインで読み書きする。 |
| skill_narrator | 完了した Skill スポーンの結果を chat 履歴にナレーションする。Skill が完了したときに `reyn chat` が自動的に起動します。直接呼び出せません。 |
| chat_compactor | 長い chat 履歴を構造化されたローリングサマリーにコンパクト化する。トークンの閾値がトリガーされると `reyn chat` が自動的に起動します。直接呼び出せません。 |

任意の Skill の完全な説明とエントリー指示を見るには `reyn skills <name>` を実行してください。
