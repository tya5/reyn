---
type: tutorial
topic: getting-started
audience: [human]
---

# 02 — Chat モード

`reyn chat` は Reyn を体感する最も摩擦の低い方法です。話しかけて、 stdlib Skill にルーティングされる様子を見て、 返ってきた答えを読む。 自分で何も作らずに済みます。

このチュートリアルは自動作成される `default` agent のみを使います。 マルチエージェントは後のトピックです（最後にポインタを示します）。

## セッションを開始する

```bash
reyn chat
```

初回実行時、 Reyn は `.reyn/agents/default/` 配下に `default` agent を自動作成します。 以降の実行ではこれを再利用します。

`>` プロンプトが表示されます。

## ターンを入力する

```
> このプロジェクトの README を要約してください
```

何が起こるか:

1. `skill_router`（stdlib Skill）が意図を分類します。
2. 最もマッチする Skill を選びます — 「README を要約」 のような要求なら、 典型的には `read_local_files` のあとに `direct_llm` が走るか、 モデルが返答内でインライン要約するなら `read_local_files` 単独で完結することもあります。
3. Skill が実行され、 プロンプトの下に答えが表示されます。
4. セッションは継続。 次のターンを入力できます。

いくつか試してみてください:

```
> このプロジェクトは何？
> src/reyn/ の中身は？
> 3 か国語で挨拶して
```

（ルーターが選ぶ Skill カタログを見たい場合は、 セッションを抜けてシェルから `reyn skills` を実行してください。 chat の会話では enumerate されません。）

各ターンは `.reyn/agents/default/` 配下に記録されます。

## 終了する

`Ctrl+D` か `/quit` でセッションを終わります。 もう一度 `reyn chat` を実行すると同じ agent が再開され、 memory と履歴は保持されています。

## スラッシュコマンド

`/` で始まる行は LLM にルーティングされず、 制御コマンドとして処理されます:

- `/list` — 実行中の Skill spawn と保留中のユーザープロンプトを表示。
- `/cancel <id>` — 実行中の Skill spawn をキャンセル（id は `/list` で取得）。
- `/answer <id> <text>` — 保留中の `ask_user` / Permission プロンプトに回答。

default モードで使うのはこの 3 つだけです。 `/agents` / `/attach` / `/plan` などは複数 agent や長時間プランを扱うようになってから役立ちます。 そのときに [reference/cli/chat](../../reference/cli/chat.md) を参照してください。

## Memory は自動

ルーターはターンごとに memory を読み（追加設定不要）、 永続化すべき内容を検出したら書き戻します。 2 層構成です:

- **Shared** — `.reyn/memory/` — すべての agent から見えるファクト。
- **Agent** — `.reyn/agents/default/memory/` — この agent にスコープされたファクト。

何が記憶されているかを確認できます:

```bash
reyn memory list
reyn memory show <slug>
```

詳細なモデルは [コンセプト/memory](../../concepts/memory.md) を参照。

## 裏で何が起きているか

OS は「chat」 という概念を知りません。 ただ Skill を実行しているだけです — それが `skill_router` で、 たまたま別の Skill（マルチエージェント構成ならピア agent）を選んで委任します。 ルーターは普通の stdlib Skill であって特別なツールではありません。 これはあなたが書く Skill と同じ合成パターンです（[P7](../../concepts/principles.md#p7-os-is-skill-agnostic-critical)）。

## 学んだこと

- `reyn chat` で自動作成された `default` agent に REPL がアタッチされる。
- 各ターンは `skill_router` を通り、 stdlib Skill が選ばれて実行される。
- Memory は 2 層（shared + agent）で、 自動的に読み書きされる。
- このステージで必要なスラッシュコマンドは `/list` / `/cancel` / `/answer` の 3 つ。

## 次に進む

Reyn が chat agent として価値を提供する様子を見ました。 ここから:

- **[チュートリアル 03 — はじめての Skill](03-your-first-skill.md)** — `skill_builder` で自分の Skill を作る。
- **[チュートリアル 04 — Skill を実行する](04-running-a-skill.md)** — 入力フォーマット / フラグ / イベントログを含む CLI 実行の詳細。
- **[チュートリアル 05 — eval を書く](05-writing-an-eval.md)** — ルーブリックで挙動を固定する。
- **マルチエージェント（あとで）:** [ハウツー: Agent チームを構築](../for-skill-authors/composition/build-an-agent-team.md) が `reyn agent new`、 役割ごとの allowlist、 `/attach` を扱います。 背景: [コンセプト/multi-agent](../../concepts/multi-agent.md)、 [コンセプト/topology](../../concepts/topology.md)。
- **なぜそうなっているか:** [コンセプト/principles](../../concepts/principles.md)。
