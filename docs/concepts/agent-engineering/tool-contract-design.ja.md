---
type: concept
topic: architecture
audience: [human, agent]
---

# Tool Contract Design

> **ステータス: 一部陳腐化。** このページは後の engine 削除で削除された
> phase-graph skill engine を前提に書かれていました。「Candidate outputs」と
> 「Preprocessor」節はそのエンジン固有の内容(`next_phase` 遷移、
> `skill_router`/preprocessor チェーン)だったため削除済みです — 現行ソースにいずれの
> 概念も存在しないことを直接 grep で確認しました。以下の「Control IR」節は維持・修正
> しています: 副作用エンベロープ自体は現在も生きていますが、`op_runtime/registry.py`
> ではなく `schemas/models.py` にあります(`OP_KIND_MODEL_MAP` は CLAUDE.md hard rule
> CLAUDE.md の OP_KIND_MODEL_MAP/control-ir.md 同期ルールによりそちらへ移設済み)。op kind 一覧も現行のものに更新しています。

LLM がどのように世界に作用するか: 副作用のための型付きエンベロープ。クリーンなツールコントラクトにより、検証とリプレイが同じ機構を共有できます。

## Reyn の実装方法

### Control IR — 副作用エンベロープ

すべての副作用(ファイル I/O、ユーザーへの問い合わせ、データの提示、サンドボックス化されたコマンド実行、MCP ツール呼び出し)は `kind` ディスクリミネーターを持つ JSON オブジェクトです。OS は各 op をその kind のスキーマに対してディスパッチします:

```json
{"kind": "read_file", "path": "src/foo.py"}
{"kind": "ask_user", "question": "Which model?", "suggestions": [...]}
{"kind": "mcp", "server": "github", "tool": "create_issue", "args": {...}}
```

op kind は `OP_KIND_MODEL_MAP`(`schemas/models.py`)に定義されています: 細粒度のファイル op(`read_file`、`write_file`、`edit_file`、`delete_file`、`glob_files`、`grep_files`)に加えて、`ask_user`、`present`、`sandboxed_exec`、`mcp`(とその resource/prompt/subscribe バリアント)、`web_search`、`web_fetch`、および RAG / task / compaction の各 kind。完全かつ現行のカタログは [Control IR](../../reference/runtime/control-ir.md) を参照してください。

## コントラクトをこれほど厳密に型付けする理由

「すべての op にスキーマがある」から 2 つの性質が生まれます:

- **早期拒否。** 不正な出力は副作用が実行される前に検証エラーを引き起こします。
- **安全なリプレイ。** 保存されたイベントログは、すべての op が書き込み時に検証されているため、LLM を再呼び出しせずに再レンダリングできます。

## 関連情報

- [リファレンス: control-ir](../../reference/runtime/control-ir.md)
- [system-design.md](system-design.md) — コントラクトが可能にすること
- [reliability-engineering.md](reliability-engineering.md) — 拒否の処理方法
