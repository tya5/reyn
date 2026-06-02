---
type: concept
topic: architecture
audience: [human, agent]
---

# Tool Contract Design

LLM がどのように世界に作用するか: 副作用のための型付きエンベロープ、決定のための型付きエンベロープ、そして LLM が呼び出される前に実行される決定論的フック。クリーンなツールコントラクトにより、検証、リプレイ、再プロンプトのすべてが同じ機構を共有できます。

## Reyn の実装方法

3 つのコントラクト、すべてスキーマに基づく:

### 1. Control IR — 副作用エンベロープ

すべての副作用（ファイル I/O、ユーザーへの問い合わせ、サブ Skill の呼び出し、シェル実行、リント）は `kind` ディスクリミネーターを持つ JSON オブジェクトです。OS は各 op をその kind のスキーマに対してディスパッチします:

```json
{"kind": "read_file", "path": "src/foo.py"}
{"kind": "ask_user", "question": "Which model?", "suggestions": [...]}
{"kind": "run_skill", "skill": "recall_memory", "input": {...}}
```

op kind は `OP_KIND_MODEL_MAP`（`op_runtime/registry.py`）に定義されています: 細粒度のファイル op（`read_file`、`write_file`、`edit_file`、`delete_file`、`glob_files`、`grep_files`）に加えて、`ask_user`、`run_skill`、`lint`、`shell`、`mcp`、`web_search`、`web_fetch`、および RAG / sandbox / compaction の各 kind。利用可能な op は `available_control_ops` として Phase ごとに LLM のコンテキストに注入されます。Phase の markdown がその構文を説明することはありません（P8）。

各 Phase はさらに frontmatter の `allowed_ops` でそのセットを絞り込みます（デフォルトは細粒度ファイル op + `ask_user`）。OS はリストされた kind のみを LLM に見せ、それ以外を LLM が出力しても拒否します。これは二重の効果があります: ドリフトを防ぎ（`write_memory` の抽出 Phase は flash-lite を見て「名前を調べよう」と思っても `web_search` を使えません）、プロンプトを縮小します（無関係な op の説明にトークンを払わずに済みます）。

### 2. Candidate outputs — 決定エンベロープ

各 Phase に対して、OS は合法的な次の手のセットを計算します: 許可された各次 Phase（または `end`）と、それが期待する入力スキーマ。LLM はその 1 つを選び、一致する artifact を生成します:

```json
{
  "control": {"type": "transition", "decision": "continue", "next_phase": "review", ...},
  "artifact": {"type": "draft", "data": {...}},
  "control_ir": [...]
}
```

形式は固定されており、ディスクリミネーターは検証され、artifact は選択したターゲットのスキーマに対してチェックされます。コントラクトから外れたものはすべて拒否されます。

### 3. Preprocessor — 決定論的エンリッチメント

Phase は LLM が呼び出される**前**に実行されるチェーンを宣言できます: サブ Skill の呼び出し、リストに対する繰り返し、スキーマに対する検証、Python 関数の実行。結果は LLM の入力の名前付きスロットに格納されます。Phase はスロット名で参照し、それが preprocessor から来ていることを知る必要はありません。

これにより、stdlib Skill は命令型コードなしに組み合わせられます: `eval` は criterion ごとのリクエストに対して `judge_phase` を fan-out し、`skill_router` はどの Skill にディスパッチするかを決める前に `recall_memory` を呼び出します。

## コントラクトをこれほど厳密に型付けする理由

「すべてにスキーマがある」から 3 つの性質が生まれます:

- **早期拒否。** 不正な出力は副作用が実行される前に再プロンプトを引き起こします。
- **安全なリプレイ。** 保存されたイベントログは、すべての artifact と op が書き込み時に検証されているため、LLM を再呼び出しせずに再レンダリングできます。
- **驚きのない組み合わせ。** サブ Skill の出力は型付き artifact であり、呼び出し元 Phase は通常の入力と同様にそれを消費します。

## まだ薄い部分

現在の 5 種類の Control IR kind はほとんどのワークフローをカバーしていますが、エコシステムが成長するにつれてさらに多くが必要になるでしょう。MCP 統合はランタイムレイヤーに存在します（Skill は Permission で MCP サーバーを宣言でき、LLM は MCP ツールを op として取得します）。サーフェスエリアは成長するでしょう。コントラクトの拡張は意図的に安価にしています: OS に kind を追加し、`available_control_ops` で宣言すれば、すべての Skill がそれを使えます。

## 関連情報

- [リファレンス: control-ir](../../reference/runtime/control-ir.md)
- [リファレンス: llm-output-contract](../../reference/runtime/llm-output-contract.md)
- [リファレンス: preprocessor](../../reference/dsl/preprocessor.md)
- [リファレンス: artifact-yaml](../../reference/dsl/artifact-yaml.md)
- [system-design.md](system-design.md) — コントラクトが可能にすること
- [reliability-engineering.md](reliability-engineering.md) — 拒否の処理方法
