---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn run-once]
---

# `reyn run-once`

ワンショットの非対話エージェント呼び出し。stdin の**全体**を 1 つの user message
として読み、general agent を完了まで駆動し（任意回数の tool-call → 1 回の停止）、
最終返信を出力して終了します。対話的な [`reyn chat`](chat.md) のバッチ / プログラム
版です — SWE-bench runner などの自動化が task 全体を 1 メッセージとして渡します。

## 書式

```
reyn run-once [agent_name] [OPTIONS] < prompt
```

prompt は stdin から全体を（行単位でなく）読みます。

## 位置引数

| 名前 | 説明 |
|------|------|
| `agent_name` | 駆動する agent。デフォルト: `default`。 |

## オプション

| フラグ | 説明 |
|------|------|
| `--max-iterations N` | 自律ループの 1 メッセージあたり tool-call 予算。デフォルト `80` — 対話 chat より高く、agent が explore → edit → verify を完了まで反復できる。 |
| `--grant-file-write` | resolver 層で `file.read` + `file.write` を付与し、非対話 agent が prompt なしで working tree を編集できる（sandbox の write-paths で bound）。`reyn chat --grant-file-write` と同じ。 |
| `--exclude-tools NAMES` | agent の LLM-visible カタログから隠すツール名（カンマ区切り、例 `web__search,web__fetch`）。`reyn chat --exclude-tools` と同じ。 |
| `--exclude-categories NAMES` | カタログソースで隠すカテゴリ名（カンマ区切り、例: task に Reyn 自身のソースが無関係なら `reyn_source`）。`reyn chat --exclude-categories` と同じ。 |

environment-backend フラグと [共通フラグ](common-flags.md) は `reyn chat` /
`reyn run` と共有です。

## 挙動メモ

- **ステートレス。** ワンショット実行は agent の永続化された会話履歴を **読み込み
  ません** — 継続すべき先行会話がないためです。スコープ付きセッション（permission
  grant・除外ツール・environment backend）は `reyn chat` と同一に構築され、最終的な
  駆動だけが異なります（行単位 REPL でなくワンショット完了）。
- **stdin 全体読み。** stdin ストリーム全体を 1 メッセージとして取るので、複数行の
  task もそのまま届きます。

## 例

デフォルト agent をパイプした prompt で実行:

```bash
echo "README を要約して open TODO を列挙して" | reyn run-once
```

working tree を編集しうる named agent を駆動:

```bash
cat task.md | reyn run-once coder --grant-file-write
```

外部リポジトリ task で web ツールと Reyn-source カテゴリを隠す:

```bash
cat task.md | reyn run-once --exclude-tools web__search,web__fetch --exclude-categories reyn_source
```

## 関連

- [`reyn chat`](chat.md) — 対話版（スコープ付きセッション構築を共有）
- [`reyn run`](run.md) — 特定の skill を端から端まで実行
- [共通フラグ](common-flags.md)
