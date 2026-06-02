---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn source]
---

# `reyn source`

`reyn run index_docs` で作成された index 済み source（名前付きドキュメントコレクション）を管理します。メンタルモデルと indexing ワークフローについては [コンセプト: RAG](../../concepts/data-retrieval/rag.md) を参照してください。

## 概要

```
reyn source list   [--json]
reyn source describe <NAME>
reyn source rm     <NAME> [-y]
```

## 説明

`reyn source` は `.reyn/index/` に保存された index 済み source を確認・削除するための主要インターフェイスです。source は `reyn run index_docs` を実行して作成されます。このコマンドグループは source の作成や更新を行いません。

---

## サブコマンド

### `list`

`.reyn/index/sources.yaml` に登録されたすべての index 済み source を一覧表示します。

```
reyn source list [--json]
```

**説明:** 各 source の名前、description、chunk 数、embedding モデル、最終 index タイムスタンプを表示します。source が index されていない場合は、使い始め方のヒントを表示します。

**オプション:**

| フラグ | 説明 |
|------|------|
| `--json` | デフォルトの表形式ではなく JSON 配列で出力します。各要素には `name`、`description`、`path`、`chunk_count`、`embedding_model`、`last_indexed`、`backend` が含まれます。 |

**例:**

```bash
reyn source list
```

出力:

```
NAME          DESCRIPTION                              CHUNKS  MODEL                    LAST INDEXED
────────────────────────────────────────────────────────────────────────────────────────────────────
memory        User notes / past session memos          142     text-embedding-3-small   2026-05-09T10:14:00Z
my_docs       Project documentation                    89      text-embedding-3-small   2026-05-10T08:30:00Z
reyn_code     Reyn Python framework source code        1247    text-embedding-3-small   2026-05-10T08:45:00Z
```

source が index されていない場合:

```
No indexed sources yet.
Try: reyn run index_docs --source <name> --path "<glob>" --description "<description>"
```

```bash
# 機械可読な出力
reyn source list --json
```

**Exit codes:**

| コード | 意味 |
|------|------|
| `0` | 成功（source が 0 件の場合も含む）。 |
| `1` | `sources.yaml` 読み取り時の I/O エラー。 |

---

### `describe <NAME>`

単一の index 済み source の詳細情報を表示します。

```
reyn source describe <NAME>
```

**説明:** 1 つの source の完全なメタデータを表示します。名前、description、path glob、chunk 数、embedding モデル、ストレージバックエンド、ストレージパス、最終 index タイムスタンプが含まれます。

**例:**

```bash
reyn source describe my_docs
```

出力:

```
Source: my_docs
  Description:     Project documentation
  Path:            docs/**/*.md
  Chunks:          89
  Embedding model: text-embedding-3-small
  Backend:         sqlite
  Index path:      .reyn/index/my_docs/index.db
  Last indexed:    2026-05-10T08:30:00Z
```

**Exit codes:**

| コード | 意味 |
|------|------|
| `0` | source が見つかり、詳細を表示した。 |
| `1` | source が見つからなかった（`<NAME>` が `sources.yaml` にない）。 |

---

### `rm <NAME>`

index 済み source とそれに関連するすべての index データを削除します。

```
reyn source rm <NAME> [-y]
```

**説明:** source の vector index をディスクから削除し（`.reyn/index/<name>/index.db`）、`.reyn/index/sources.yaml` の対応するエントリを削除します。source は `reyn source list` に表示されなくなり、LLM のシステムプロンプトにも表示されなくなります。

デフォルトでは削除前に確認を求めます。`-y` を使うと確認をスキップできます。

内部的に `index_drop` op を呼び出します。これには `permissions.index_drop` パーミッション（デフォルト: `ask`）が必要です。事前に承認されていない場合、初回実行時に一度プロンプトが表示されます。

**オプション:**

| フラグ | 説明 |
|------|------|
| `-y`、`--yes` | 確認プロンプトをスキップします。スクリプトや indexing 戦略を試行錯誤する場合に便利です。 |

**例:**

```bash
# インタラクティブ — 確認プロンプトを表示
reyn source rm my_docs
```

出力:

```
Remove source 'my_docs' and delete .reyn/index/my_docs/index.db? [y/N] y
Source 'my_docs' removed.
```

```bash
# プロンプトをスキップ
reyn source rm my_docs -y
```

```bash
# 典型的な試行ワークフロー: 削除して別の戦略で再 index
reyn source rm my_docs -y
reyn run index_docs --source my_docs --path "docs/**/*.md" --description "プロジェクトドキュメント"
```

**Exit codes:**

| コード | 意味 |
|------|------|
| `0` | source を正常に削除した。 |
| `1` | source が見つからない、または index ファイルの削除や `sources.yaml` の更新時に I/O エラーが発生した。 |

---

## 関連項目

- [コンセプト: RAG](../../concepts/data-retrieval/rag.md) — indexing ワークフロー、source モデル、chunker の概要
- [`reyn run index_docs`](run.md) — source index を作成または更新する
- [`recall` ツール](../../concepts/data-retrieval/rag.md) — LLM 向けの retrieval ツール
- [コンセプト: パーミッションモデル](../../concepts/runtime/permission-model.md) — `index_drop` パーミッションゲート
