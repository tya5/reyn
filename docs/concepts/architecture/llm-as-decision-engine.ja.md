---
type: concept
topic: architecture
audience: [human, agent]
---

# LLM を制約された意思決定エンジンとして使う

reyn では LLM はオーケストレーターではありません。OS が遷移の間に呼び出す **意思決定ノード** です。OS は小さく有限な選択肢のセットを渡し、LLM がその中から 1 つを選びます。セット外のものはすべて拒否されます。

## LLM が選べるもの

各 Phase 訪問ごとに、OS はコンテキストフレームを構築します：

- 現在の Phase の instructions と input artifact、
- **candidate outputs** — 許可された次の Phase（または `end`）ごとに、その Phase が期待する input スキーマ、
- **available Control IR ops** — この Phase でアンロックされているサイドエフェクト。

LLM は 1 つの JSON オブジェクトで応答します：

- `control` — 1 つの候補を選ぶ（Phase への `transition`、または `finish`）、
- `artifact` — 選んだ遷移先の input スキーマに適合するデータ、
- `control_ir` — 利用可能なリストから 0 個以上のサイドエフェクト ops。

これがコントラクトです。他のチャンネルはありません。

## 「OS が LLM を呼び出すツール」という見方が正確でない理由

むしろ逆のフレーミングの方が正確です。LLM は **意思決定ポリシー** であり、OS が制約された行動空間を提供します。OS は LLM のツールではなく、LLM ができることを制限するルール管理者です。

この制限が reyn の 3 つの保証をもたらします：

- **再生可能。** 保存された event ログがワークフローを完全にキャプチャします。同じ入力での再実行は同じエッジをたどります（各 Phase 内の LLM の確率性は除く）。
- **バリデーション可能。** すべての artifact は OS が遷移をコミットする前に遷移先スキーマに対してチェックされます。不正な出力はクラッシュやサイレントなドリフトではなく、再プロンプトを引き起こします。
- **拡張可能。** LLM は OS が注入した候補セットからしか選べないため、新しい Phase や新しい control op を追加しても再トレーニングやプロンプトエンジニアリングは不要です。OS がもう 1 つのオプションを公開するだけです。

## 「LLM が間違えたら？」のケース

| LLM が出力するもの | OS の動作 |
|----------------|-------------|
| グラフに無い `next_phase` | 拒否。`validation_error` を発行。再プロンプト |
| `type` が一致しない `artifact` | 拒否。`validation_error` を発行。再プロンプト |
| 必須スキーマフィールドが欠けている | 拒否。`validation_error` を発行。再プロンプト |
| Phase が宣言していない Control IR ops | 拒否。`permission_denied` を発行 |
| JSON コントラクト外の自由形式テキスト | ノーマライザーが回復を試みる。失敗すれば `normalization_error` を発行 |

設定可能な回数の再プロンプト失敗後、実行は中断されます。OS は LLM の出力をサイレントに修正することは絶対にありません。

## なぜ LLM に自由を与えないのか

無制限の LLM 制御フローは 3 つの測定可能な点で不安定です：

1. **長い実行でのドリフト。** 各自由な選択がタスクから外れる機会になります。選択肢セットを制限することで、軌跡がワークフローの設計内に収まります。
2. **テスト不能性。** 「このプロンプトは最終的に終了するか？」は自由エージェントには決定不能ですが、有限グラフでは自明に決定可能です。
3. **クリーンな再エントリーポイントがない。** 何かがおかしいとき、障害を起こした Phase を指し示したいものです。自由形式のオーケストレーションには指し示す Phase がありません。

reyn は Skill グラフを明示的に書くコストを払い、代わりに予測可能性を手に入れます。

## 参考

- [../architecture/principles.md](../architecture/principles.md) — P3、P4、P8
- [../architecture/phase-vs-skill-vs-os.md](../architecture/phase-vs-skill-vs-os.md)
- [Reference: llm-output-contract](../../reference/runtime/llm-output-contract.md)
- [Reference: context-frame](../../reference/runtime/context-frame.md)
