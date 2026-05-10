---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn chat]
---

# `reyn chat`

agent にアタッチされたインタラクティブな REPL セッションを開始します。各ユーザーターンは `skill_router` stdlib Skill を通じてディスパッチされ、意図を分類して直接返信、プロジェクト/stdlib Skill の実行、または別の agent への委任を行います。

Memory の検索と書き込みはルーター Phase の内部で自動的に行われます。[コンセプト/memory](../../concepts/memory.md) を参照してください。

## 概要

```
reyn chat [agent_name] [OPTIONS]
```

`agent_name` は位置引数でオプションです。省略すると、Reyn は自動作成された `default` agent にアタッチします。

## オプション

| フラグ | 説明 |
|------|-------------|
| `--model MODEL` | このセッションのモデルクラスまたは LiteLLM モデル文字列。デフォルトは `reyn.yaml` から。 |
| `--output-language LANG` | 出力言語コード。デフォルトは `reyn.yaml` から。 |
| `--max-phase-visits N` | ターンごとの単一 Phase 再訪問の上限。`0` = 無制限。 |

## agent Workspace

各 agent は `.reyn/agents/<name>/` 配下に状態を永続化します:

- `profile.yaml` — 名前、ロール、オプションの `allowed_skills`（[リファレンス](../dsl/profile-yaml.md)）
- `history.jsonl` — 追記専用の会話ログ（chat + agent 間メッセージ、クロス agent トレース用の chain_id 付き）
- `events.jsonl` — `reyn events` 用のランタイムイベント
- `memory/` — agent スコープの Memory レイヤー（`MEMORY.md` + body ファイル）
- `runs/` — 起動された Skill のランの Workspace

前の会話を再開するには、同じ agent にアタッチします:

```bash
reyn chat researcher
```

`default` agent は常に存在します。[`reyn agent new`](agent.md) でさらに作成します。

## スラッシュコマンド

セッションがアクティブな間、`/` で始まる行は処理され、agent にルーティングされません。

| コマンド | 効果 |
|---------|--------|
| `/list` | 実行中の Skill スポーンと保留中の介入を表示 |
| `/cancel <id>` | Skill スポーンをキャンセル（完全な id または最後の 4 文字） |
| `/answer <id> <text>` | 保留中の `ask_user` / Permission プロンプトに回答 |
| `/agents` | 読み込まれた agent と現在アタッチされているものを一覧表示 |
| `/attach <name>` | REPL ポインターを別の agent に切り替える（前の agent はバックグラウンドで実行し続ける） |
| `/skill list` | 実行中の Skill 実行を表示（id / 名前 / current_phase + 親子関係） |
| `/skill discard <run_id>` | 特定の Skill 実行を中止して cleanup を実行 |
| `/plan list` | 実行中の Plan を表示（動作中 task と resume 待ちを組み合わせて表示） |
| `/plan discard <plan_id>` | 特定の Plan を中止して cleanup を実行。R-D14 経由で待機中の peer agent に通知 |
| `/plan resume <plan_id> --from <step_id>` | 特定 step から Plan を再実行するオペレーター向け escape hatch（ADR-0023 §3.7） |
| `/tasks` | Skill 実行と Plan task を横断する統合ビュー（FP-0012）。`/tasks list` と同じ |
| `/tasks status <prefix>` | 特定 task の current phase + 経過時間を表示（Skill / Plan どちらでも prefix で解決） |
| `/tasks kill <prefix>` | 特定 task を中止。prefix は Skill の run_id と Plan の plan_id 両方とマッチ |

`/list` / `/cancel` / `/answer` は基盤となります。複数の Skill 実行と介入がプロンプトをブロックせずに共存できます。`/agents` / `/attach` はマルチエージェントワークフローのプリミティブです。`/skill` / `/plan` は crash recovery オペレーターコマンドで、per-skill-run / per-plan-run のライフサイクルを surface します。`/tasks` はその両方を横断する統合エントリーポイントで、Skill が spawn された後に LLM が user に `/tasks` で進捗確認を案内します（FP-0012 chat-mode 非同期 dispatch）。

## マルチエージェントの動作

ルーターがこのターンは別の agent が処理した方がよいと判断した場合、`skills_to_run` エントリーの代わりに（またはそれに加えて）`messages_to_agents` エントリーを出力します。受信 agent はリクエストを非同期に処理します。返信は発信元のチェーンに自動ルーティングされて戻ります。完全なモデルについては [コンセプト/multi-agent](../../concepts/multi-agent.md) を参照してください。

ユーザーが開始したチェーンは中間の `reply_text`（発信元 agent の最初のルーターターン）を発行し、その後デリゲートのレスポンスが届いた後に最終的な統合された返信が続きます。これにより、ホップをまたいでも「作業中です」という UX が保たれます。

`/attach` スラッシュコマンドでチェーン途中のデリゲートの進捗を監視できます。前の agent の `session.run()` は受信トレイを消費し続けるので、後で戻っても問題なく解決されます。

## Permission の動作

`reyn chat` はインタラクティブです。サブ Skill がデフォルト外の Permission を必要とする場合、介入キュー経由で応答するまでプロンプトがブロックします。選択は `.reyn/approvals.yaml` に永続化できます（[permissions リファレンス](../config/permissions.md) を参照）。

## 例

デフォルト agent に対して新しいセッションを開始:

```bash
reyn chat
```

名前付き agent にアタッチ:

```bash
reyn chat researcher
```

この会話のみにより強力なモデルを使用:

```bash
reyn chat --model strong
```

## 関連情報

- [リファレンス: agent CLI](agent.md) — `reyn agent new / list / show / rm`
- [リファレンス: topology CLI](topology.md) — `reyn topology` で通信構造を宣言
- [リファレンス: skill_router](../stdlib/skill_router.md)
- [リファレンス: profile-yaml](../dsl/profile-yaml.md)
- [リファレンス: multi-agent 設定](../config/multi-agent.md) — `multi_agent.max_hop_depth`
- [リファレンス: state-dir](../config/state-dir.md) — `agents/` の場所
- [コンセプト: multi-agent](../../concepts/multi-agent.md)
- [コンセプト: memory](../../concepts/memory.md)
- [コンセプト: plan-mode](../../concepts/plan-mode.md)
- [コンセプト: skill-resume](../../concepts/skill-resume.md)
