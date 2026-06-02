---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [phases/*.md]
---

# `phase.md` frontmatter

各 Phase は Skill ディレクトリの `phases/<phase_name>.md` に存在します。YAML frontmatter は Phase が消費するもののみを宣言します。生成するもの、次の Phase、何も宣言しません（[P1](../../concepts/architecture/principles.md)）。

## スキーマ

```yaml
---
type: phase                    # 常に "phase"
name: <phase_name>             # ファイル名と一致する必要があります（.md 拡張子を除く）
input: <artifact_type>         # 必須; この Phase が消費するもの
role: <short_label>            # 省略可能; Events 用の一単語のロール
can_finish: true               # 省略可能; ここから終了を許可（デフォルト: false）
allowed_ops: [file, ask_user]  # 省略可能; この Phase が使用できる Control IR op の種類
                                # （デフォルト: ["file", "ask_user"]; [] はop なし）
default_sandbox_policy:        # 省略可能; この Phase の全 sandboxed_exec op に
  network: true                # 適用される SandboxPolicy。op 自身の fields に
  read_paths: ["/"]            # 優先する（LLM は上書き不可）
  write_paths: ["/"]
  allow_subprocess: true
  env_passthrough: [PATH, HOME]
  timeout_seconds: 600
preprocessor:                  # 省略可能; 決定論的な LLM 前処理ステップ
  - run_skill:
      skill: recall_memory
      input: { type: ..., data: { ... } }
      into: relevant_memories
  - python:
      module: stats
      function: compute
      mode: safe                # safe | unsafe
      output_schema: { ... }
---
```

## 必須フィールド

- **`type`** — `phase` でなければなりません。
- **`name`** — 文字列、Skill の `graph` でこの Phase を識別します。ファイル名と一致する必要があります。
- **`input`** — Phase が読み取る artifact 型。単一の artifact 名またはユニオン（`user_message | topic_input`）。

## 省略可能なフィールド

- **`role`** — イベントペイロード用の短いラベル（例: `planner`、`reviewer`）。
- **`can_finish`** — `true` の場合、LLM はこの Phase から `decision="finish"` を出力できます。OS は最終 artifact を Skill の `final_output_schema` に対して検証します。
- **`preprocessor`** — LLM 呼び出しの前に実行される決定論的ステップのチェーン。`reference/dsl/preprocessor.md` を参照してください。
- **`allowed_ops`** — この Phase が出力できる Control IR op の種類のリスト（例: `[file, lint]`）。OS は LLM に提示する `available_control_ops` をこのセットに絞り込み、*さらに* LLM がセット外の op を出力した場合も `control_ir_skipped: not_allowed_in_phase` で拒否します。デフォルト: `["file", "ask_user"]`（ファイル I/O とユーザー確認、一般的なケース）。明示的な空リスト（`[]`）は「op なし」を意味します（純粋なルーティング/採点 Phase に使用します）。リストが狭いほど、op の説明に費やされるコンテキストが少なく、Phase の意図から LLM が逸脱する余地が減ります。メタ Skill（`skill_builder`、`skill_improver`、`skill_importer`）は ContextFrame の `op_catalog` フィールド（OS がサポートするすべての op の参照リスト）を参照して、生成する Phase の `allowed_ops` 値を選択します。
- **`default_sandbox_policy`** — [`SandboxPolicy`](../../concepts/runtime/permission-model.md) の kwargs（`network`、`read_paths`、`write_paths`、`allow_subprocess`、`env_passthrough`、`timeout_seconds`）の省略可能なマッピング。設定すると、OS はこの Phase が実行する**すべての** `sandboxed_exec` op に適用し、op 自身の policy fields を上書きします — policy は決定論的になり、LLM は弱めることも強めることもできません。省略時は各 op 自身の fields が使われます。これは policy（=何が許可されるか）のみで、作業ディレクトリと backend は run コンテキストでありここでは宣言しません。workspace 結合型 backend（例: コンテナ `EnvironmentBackend`）は、コンテナ自体が分離境界である場合 policy を完全に無視することがあります。

> **注意:** Phase レベルの `permissions:` は skill-only permissions migration で廃止されました。パーミッションは skill-md frontmatter で宣言してください — [skill-md.md](skill-md.md) および [permission-model.md](../../concepts/runtime/permission-model.md) を参照してください。

## 現れてはいけないもの

- いかなる種類の出力スキーマ。出力は次の Phase の入力または Skill の `final_output` によって決まります（[P1](../../concepts/architecture/principles.md#p1-phase-is-stateless-and-reusable)）。
- 次の Phase の名前。Skill グラフがトランジションを所有します（[P1](../../concepts/architecture/principles.md#p1-phase-is-stateless-and-reusable)）。
- Control IR フォーマットの説明。OS が利用可能な op をコンテキストフレームに注入します（[P8](../../concepts/architecture/principles.md#p8-phase-instructions-contain-only-domain-logic)）。

## ボディ

Markdown ボディは LLM への Phase の指示です。以下をカバーします:

- 何を分析、生成、または決定するか
- どの next-phase 候補をいつ選ぶか
- ドメイン固有のルール、例、エッジケース

スキーマの再述、フィールド名の列挙、Control IR の説明は避けてください。これらはランタイムで注入されます。

## 例

```yaml
---
type: phase
name: outline
input: topic_input
role: planner
---

トピックの最も重要な角度を捉えた 3 つの箇条書きを作成してください。
各箇条書きは完全な文である必要があります。次の Phase が各箇条書きを
段落に展開するので、曖昧な箇条書きは曖昧な段落を生みます。

避けること: メタコメンタリー、スコープの前置き、3 つ以上の箇条書き。
```

## 関連情報

- [skill-md.md](skill-md.md) — Skill frontmatter
- `reference/dsl/preprocessor.md` — preprocessor ステップ
- [コンセプト: principles P1、P8](../../concepts/architecture/principles.md)
