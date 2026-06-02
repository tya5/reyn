---
type: reference
topic: runtime
audience: [human, agent]
---

# Context Frame

Context Frame は OS が各 Phase 訪問時に LLM に渡す読み取り専用のペイロードです。訪問ごとに再生成され、永続化されません。LLM が次の決定を行うために必要なすべてを含んでいます。

## 形式

```json
{
  "current_phase": "<phase_name>",
  "current_phase_role": "<role>",
  "instructions": "<phase markdown body>",
  "input_artifact": {"type": "<artifact_type>", "data": { ... }},
  "execution": {
    "path": ["phase_a", "phase_b"],
    "current_visit": 1,
    "total_steps": 3
  },
  "candidate_outputs": [
    {
      "next_phase": "<phase_name> or end",
      "control_type": "transition | finish",
      "schema_name": "<artifact_type>",
      "artifact_schema": { ... },
      "description": "..."
    }
  ],
  "finish_criteria": [],
  "constraints": {
    "max_phase_visits": 25
  },
  "available_control_ops": [
    {"kind": "read_file", "description": "...", "example": { ... }}
  ],
  "output_language": "en"
}
```

## フィールド

### `current_phase`、`current_phase_role`

現在実行中の Phase の名前とオプションのロール。

### `instructions`

Phase ファイルの完全な Markdown ボディ。**frontmatter なし。** スキーマ情報の注入なし。ボディは人が書いたガイダンスをそのまま使用します。

### `input_artifact`

この Phase が消費する artifact。preprocessor ステップが実行された後、artifact はその場でエンリッチされます。追加のキー（例: `relevant_memories`、`python` ステップの出力）は宣言された `into` キーの下に現れます。

### `execution`

これまでのランのトレース:

- `path` — 順番に入った Phase。
- `current_visit` — `current_phase` の訪問カウント。
- `total_steps` — ランを通じての総 Phase 訪問数。

### `candidate_outputs`

この Phase から OS が許可するトランジションのセット。各エントリーは以下を含みます:

- `next_phase` — ターゲット Phase 名、または末端トランジションの `end`。
- `control_type` — `transition` または `finish`。
- `schema_name` — 期待される artifact 型。
- `artifact_schema` — JSON スキーマフラグメント。
- `description` — この候補を選ぶタイミングの一行サマリー。

LLM はこれらの 1 つを選ばなければなりません。幻覚の Phase 名は拒否されます。

### `finish_criteria`

`skill.md` からの自由形式の箇条書きリスト。終了するかどうかを決定する Phase が使用します。

### `constraints.max_phase_visits`

`reyn.yaml` または `--max-phase-visits` からの単一 Phase ごとの再訪問上限。`null` は無制限を意味します。

### `available_control_ops`

LLM が出力できる Control IR op の種類のセット。説明と例付き。**これが op の存在に関する唯一の情報源です。** Phase の Markdown は op の構文を説明してはなりません（P8）。

### `output_language`

自然言語出力のターゲット言語。`reyn.yaml` または `--output-language` から。

## フレームに含まれないもの

- 他の Phase の artifact（必要な場合は `file` op またはサブ Skill を使用してください）。
- イベントログ。
- Memory や他の長期状態（入力 artifact に検索されたものだけ）。
- LLM 自身の過去の出力（各呼び出しはステートレスです。preprocessor + このフレームが唯一のコンテキストです）。

## この設計の理由

訪問ごとに新鮮なフレームを構築することで、すべての Phase を自己完結させます。訪問間に隠れた会話状態はありません。OS が注入するもののみです。これがランを再現可能にし、個々の Phase を Skill をまたいで再利用できる理由です。

## 関連情報

- [llm-output-contract.md](llm-output-contract.md) — LLM が返すものの形式
- [control-ir.md](control-ir.md) — `available_control_ops` op の種類
- [コンセプト: architecture](../../concepts/architecture.md)
