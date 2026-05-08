---
type: tutorial
topic: getting-started
audience: [human]
---

# 05 — Chat モード

`reyn chat` は *agent* にアタッチされたインタラクティブな REPL です。各ターンは `skill_router` を通り、意図を分類して返信、Skill の実行、または他の agent への委任を行います。Memory は自動的に検索・書き込みされます。

## セッションを開始する

```bash
reyn chat
```

これで自動作成された `default` agent にアタッチされます。特定の名前の agent にアタッチするには:

```bash
reyn chat researcher
```

ターンを入力します:

```
> このプロジェクトの README を要約してください
```

ルーターは `text_summarizer`（またはその他最もよくマッチする stdlib/project Skill）を選択し、実行して結果を表示します。各ターンは `.reyn/agents/<name>/` 配下に永続化された同じセッションにとどまります。

## スラッシュコマンド

`/` で始まる行はルーティングされず、制御コマンドとして処理されます:

- `/list` — 実行中の Skill スポーンと保留中の介入
- `/cancel <id>` — Skill スポーンをキャンセル
- `/answer <id> <text>` — 保留中の `ask_user` / Permission プロンプトに回答
- `/agents` — このプロセスに読み込まれた agent を一覧表示
- `/attach <name>` — REPL を別の agent に切り替える

## 複数の agent

独自の役割と Skill allowlist を持つ名前付き agent を立ち上げられます:

```bash
reyn agent new researcher --role "deep technical research, prefers primary sources"
reyn agent new writer     --role "concise long-form prose"
```

`default` にアタッチされた chat セッションでは、ルーターがリクエストを `researcher` が処理した方がよいと判断し、委任を出力することがあります。返信は自動的にルーティングされて戻ります。中間の確認の後、統合された最終回答が表示されます。チェーン中の進捗を監視するには `/attach researcher` を使用します。

通信できる相手に関する構造的な制限については、[topology CLI](../../reference/cli/topology.md) と [コンセプト/topology](../../concepts/topology.md) を参照してください。

## ルーターの選択方法

`skill_router` は `user_message`、利用可能な Skill（`profile.allowed_skills` が設定されている場合はフィルタリング済み）、到達可能なピア agent（topology ルールによるフィルタリング済み）、マージされた Memory インデックスを読み取ります。Skill、agent、直接返信のいずれか 1 つのパスを選択します。特定の Skill を使用させたい場合は明示的に（「skill_builder を使って...」）依頼してください。ルーターはその手がかりを使います。

## Memory は自動

ルーター Phase はすべてのターンで 2 つの Memory レイヤーを読み取ります（追加設定不要）:

- **Shared** — `.reyn/memory/` — すべての agent に見えるファクト
- **Agent** — `.reyn/agents/<name>/memory/` — この agent にスコープされたファクト

書き込みは永続化すべき何かを検出した同じルーターターン内で行われます。完全なモデルについては [コンセプト/memory](../../concepts/memory.md) を参照してください。

## Memory の確認と管理

`reyn memory` CLI はデフォルトで **shared** レイヤーを操作します:

```bash
reyn memory list             # 保存されたすべての Memory を表示
reyn memory show <slug>      # 1 つを表示
reyn memory edit <slug>      # $EDITOR で開く
reyn memory delete <slug>    # 削除
```

agent スコープのレイヤーを操作するには `--agent <name>` を渡します:

```bash
reyn memory list --agent researcher
reyn memory delete --agent researcher feedback_arxiv
```

変更コマンド（`edit`、`delete`、`import`）は変更後に自動的にレイヤーの `MEMORY.md` を再構築します。インデックスがディスク上の body ファイルから乖離することはありません。

## chat モードがルーター Skill に過ぎない理由

OS は「chat」を知りません。ただ Skill を実行するだけです。`skill_router` は通常の stdlib Skill で、たまたま委任先として別の Skill（またはピア agent）を選択します。これは他の Reyn Skill と同じ組み合わせパターンです（P7）。

## 学んだこと

- `reyn chat [agent_name]` で REPL を agent にアタッチします。
- スラッシュコマンドでスポーン、介入、agent の切り替えを管理します。
- ルーターはピア agent に委任できます。チェーンはユーザーに統合されて戻ります。
- Memory は 2 層（shared + agent）で、自動的に読み書きされます。

## 次に進む

Skill の作成、実行、eval、chat をカバーしました。ここから:

- **マルチエージェント**: [コンセプト/multi-agent](../../concepts/multi-agent.md) と [コンセプト/topology](../../concepts/topology.md) を読んで、specialist agent をチームに組み合わせる方法を学びます。
- **本物のものを構築する。** プロンプトベースのワークフローの 1 つを複数 Phase の Skill に置き換えます。
- **[principles](../../concepts/principles.md) を読む。** 8 つの原則を理解すると、リファレンス内のすべてが意味をなします。
- **[how-to](../for-skill-authors/validate-artifacts.md) を見る。** 最初に出てきた具体的なニーズに合ったガイドを選びます。
