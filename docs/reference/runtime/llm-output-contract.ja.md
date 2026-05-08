---
type: reference
topic: runtime
audience: [human, agent]
---

# LLM 出力コントラクト

Skill に関わらず、すべての Phase は LLM がこのスキーマに一致する単一の JSON オブジェクトを返すことを期待します。準拠しない出力は拒否されます。OS は `validation_error` イベントを発行して再プロンプトします（リトライ制限に従います）。または Phase を失敗させます。

## スキーマ

```json
{
  "control": {
    "type": "transition | finish | abort",
    "decision": "continue | finish | abort",
    "next_phase": "<phase_name> or null",
    "confidence": 0.0,
    "reason": {"summary": "one-sentence explanation"}
  },
  "artifact": {
    "type": "<schema_name>",
    "data": { ... }
  },
  "control_ir": []
}
```

## `control` ブロック

### `type`

LLM がリクエストしているトランジションの形式。

- `transition` — 別の Phase に移動する。
- `finish` — ワークフローをクリーンに終了する。artifact は Skill の `final_output_schema` に一致しなければなりません。
- `abort` — 回復不能なエラー。artifact は空でも構いません。

### `decision`

OS レベルの意図。**有効な値は 3 つだけです。** `revise` のような Skill 固有の動詞は許可されません（P7）。

- `continue` — 通常のトランジション。`revise` Phase にトランジションする修正ループを含む、あらゆる非末端フローで有効。
- `finish` — 終了。`type=finish` と `next_phase=null` が必要。
- `abort` — エラー終了。`type=abort` と `next_phase=null` が必要。

### `next_phase`

- `type=transition` の場合、Skill グラフに基づいて現在の Phase の許可された次 Phase のいずれかでなければなりません。
- `type=finish` または `type=abort` の場合、`null` でなければなりません。

### `confidence`

`[0.0, 1.0]` の浮動小数点数。テレメトリ用。ディスパッチには影響しません。

### `reason.summary`

一文の根拠。イベントログに格納されます。

## `artifact` ブロック

- `type` — artifact スキーマ名。トランジションの場合は `next_phase` の入力スキーマに、完了の場合は Skill の `final_output_schema` に一致しなければなりません。
- `data` — artifact のペイロード。スキーマに対して検証されます。

`--strict` モードでは、必須フィールドはすべてのネストレベルで強制されます。デフォルトの寛容モードでは、トップレベルの必須フィールドのみが強制されます。

## `control_ir` ブロック

副作用 op のリスト。各 op は順番にディスパッチされます。[control-ir.md](control-ir.md) を参照してください。

## 整合性ルール

これらはディスパッチ前にチェックされます。違反は拒否されます。

- `type=finish` ⇔ `decision=finish` ⇔ `next_phase=null`。
- `type=transition` ⇔ `decision=continue` ⇔ `next_phase` は null でない、許可された Phase。
- `type=abort` ⇔ `decision=abort` ⇔ `next_phase=null`。
- `artifact.type` が選択したターゲットが示すスキーマに一致する。

## このコントラクトが厳格である理由

OS の仕事は LLM 駆動の制御フローを安全にすることです。Phase 名を幻覚した出力、decision の動詞を発明した出力、不正な artifact を返した出力を拒否することで、Reyn はランタイムが Skill 作者が想定していない状態に漂流することを防ぎます。LLM は artifact の*内部*で自由に創造的であることができます。*どの artifact* や *どの Phase* かについては決してそうであってはなりません。

## 関連情報

- [context-frame.md](context-frame.md) — LLM が見るもの
- [control-ir.md](control-ir.md) — Control IR op スキーマ
- [コンセプト: principles P4](../../concepts/principles.md#p4-llm-is-a-constrained-decision-engine)
