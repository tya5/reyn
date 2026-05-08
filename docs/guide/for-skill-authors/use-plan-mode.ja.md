---
type: how-to
topic: composition
audience: [human]
applies_to: [reyn chat]
---

# マルチステップタスクに Plan mode を使う

**目的:** ユーザーが chat で「X を調査して、要約して、ドキュメントを書いて」のようなマルチステップのタスクを依頼したとき、Plan mode はエージェントがリクエストを順序付きステップに分解し、バックグラウンドで非同期に dispatch し、各 step が完了するごとに進捗を stream で返す仕組みです。完了済みの状態は再起動をまたいで保持され、オペレーターは実行途中に介入したり方向を変えたりできます。

## いつ使うか

以下の場合に Plan mode を使います:

- ユーザーのリクエストが自然に 3 つ以上の順序付きステップに分解できる。
- 一部の step が遅い — web fetch、複数ファイル解析、sub-skill chain など。
- セッションが中断される可能性があり、crash recovery が重要。
- オペレーターが実行中に進捗を確認したり、方向を変えたりしたい。

以下の場合は Plan mode を期待しません:

- 単一ショットのプロンプトや高速な直接 LLM 応答。
- 単純な `run_skill` の合成で済む 2 ステップのタスク — [「`run_skill` で Skill を合成」](compose-skills-with-run-skill.md) を参照。

## Plan mode のトリガー方法

Plan mode は、router LLM がそのカタログから `plan` tool を選択したときに起動されます。slash command ではなく、クエリの複雑さに基づいてエージェントが判断します。よくトリガーされる表現:

- 「ステップバイステップで...」
- 「まず X、次に Y、そして Z」
- 長い open-ended なゴール: 「AI エージェントに関する調査サマリーを書いて」

**例:**

ユーザーメッセージ:

```
オープンソースのエージェントフレームワーク上位 3 つを調査し、設計思想を
比較して、チームに共有できる 500 字のサマリーを書いてください。
```

router LLM が `plan` を呼び出して分解を生成します:

```
step_1: search — LangChain / AutoGen / CrewAI の概要を収集
step_2: analyze — 3 つの設計思想を比較
step_3: write — 500 字サマリーを作成（step_1 / step_2 に依存）
```

chat turn は即座に終了します（非同期 dispatch — turn は block しません）。step が完了するごとに、オペレーターは status message を受け取ります:

```
[plan abc1] step_1 完了
[plan abc1] step_2 完了
[plan abc1] step_3 完了 — 結果を配送
```

terminal step の出力は通常の agent message として届きます。

## 何が起きているかを確認する

plan が進行中に利用できる slash command が 3 つあります。構文は [`reference/cli/chat.md`](../../reference/cli/chat.md) で確認済みです。

### `/plan list`

```
/plan list
```

全ての active な plan run を表示します — 実行中のタスクと、crash 後に auto-resume を待っている plan の両方。`plan_id` と現在の active な `step_id` を取得するためにまずこれを使います。

### `/plan discard <plan_id>`

```
/plan discard abc1
```

plan を abort し、asyncio task をキャンセルし、state（decomposition artifact + snapshot）を削除し、plan の chain で待っている peer agent に通知します。plan が意図しない方向に進んでいてクリーンな状態から始めたいときに使います。

### `/plan resume <plan_id> --from <step_id>`

```
/plan resume abc1 --from step_2
```

surgical escape hatch です（ADR-0023 §3.7）。`step_id` 以降の記録済み結果をクリアし、それより前の step を保持したまま再起動します。対象より前の step は LLM コストなしで memo replay され、対象 step 以降は新たに再実行されます。特定の step が誤った出力を生成し、plan 全体を再実行せずにやり直したいときに使います。

このコマンドは以下をエラーとして拒否します:
- 未知の plan ID。
- decomposition artifact が欠落している場合（代わりに `/plan discard` に誘導）。
- plan に存在しない step ID（有効な ID を列挙）。

## State の永続化 — crash に耐える内容

`reyn chat` プロセスが plan 実行中に終了した場合、再起動するとエージェントが自動的に resume します。保持される内容:

| State | crash 跨ぎ |
|---|---|
| plan の decomposition（step 形状） | 残る |
| step ごとの進捗と結果 | 残る |
| 32 KB 以下の step output | 残る — snapshot に inline 保存 |
| 32 KB 超の step output | 残る — per-plan workspace file に spill（ADR-0024） |
| active な asyncio.Task | 失う — 再起動時に再作成 |

次回起動時に `AgentRegistry.restore_all` が WAL を replay し、各 step を完了済みまたは pending に分類し、`PlanRuntime` task を spawn します。完了済み step は memo replay（ADR-0025 に従い LLM コストなし）され、pending step のみ再実行されます。

decomposition artifact は planner LLM を通じて再生成されません — 再分解は非決定論的で step ID が変わり memoization が壊れます。artifact が欠落または破損している場合、coordinator は自動的に discard してoutbox notice を出します。

この挙動の背景にある概念モデルは [concepts/plan-mode.md](../../concepts/plan-mode.md) を参照してください。

## オペレーターの介入レシピ

**step の出力が誤っている。** 対象の skill やプロンプトを修正した後、`/plan resume <plan_id> --from <step_id>` を使います。それより前の step は memo から replay され、対象以降の step のみ再実行されます。

**plan が意図しない方向に進んでいる。** `/plan discard <plan_id>` でクリーンに abort し、プロンプトを改善して再度リクエストします。

**2 つのマルチステップタスクを並行実行する。** plan 進行中に 2 つ目の open-ended なリクエストを開始します。各 plan は独立した `plan_id` と `chain_id` を持ちます。outbox message は投入順ではなく完了順に届きます — 短い plan が先に完了すれば先に届きます。`meta.plan_id` フィールドで返答を識別できます。

**何が起きたかをトレースする。** plan 完了（または失敗）後、イベントログを確認します:

```bash
reyn events .reyn/agents/<name>/events.jsonl --filter plan_step_completed
```

各 `plan_step_completed` イベントには `step_id`、実行時間、結果が memo replay かフレッシュな計算かが含まれます。

## よくある落とし穴

**「なぜ plan が auto-resume されないのか」** `reyn.yaml` を確認してください。`plan_resume.default` キーが動作を制御します: `retry_pending`（デフォルト）は pending step を resume し、`discard` は abort してユーザーに再発行を促す notice を出します。このキーが `discard` に設定されていれば、auto-resume は意図的に無効化されています。

**「step の出力が切り詰められているように見える」** inline output は 32 KB が上限で、それを超える結果は per-plan workspace ディレクトリのファイルに spill されます（ADR-0024）。これはデータロスではありません — 完全な出力は `agents/<name>/state/plans/<plan_id>/step_results/<step_id>.txt` にあります。`get_step_result` accessor が inline と spilled を透過的に解決します。

**「再起動時に `plan_aborted` イベントが見える」** `plan_resume.default` が `discard` のとき、プロセスが終了した時点で進行中だった plan は次回起動時に outbox notice として出ます。デフォルトの `retry_pending` ポリシーでは代わりに `plan_resumed` が表示されます。

**「plan が細かすぎる / 粗すぎる」** エージェントに自然言語で再計画を依頼します: 「ステップが多すぎる — 調査と分析を一つにまとめて」。またはステップ数のヒントを与えます: 「ちょうど 2 ステップでやってください。」 planner LLM は自然言語の分解ヒントに応答します。

## 関連情報

- [concepts/plan-mode.md](../../concepts/plan-mode.md) — 概念モデル: 非同期 dispatch、crash 分類、resume policy、multi-plan の順序付け。
- [reference/cli/chat.md](../../reference/cli/chat.md) — `/plan` ファミリーを含む完全な slash command リファレンス。
- [compose-skills-with-run-skill.md](compose-skills-with-run-skill.md) — マルチステップ plan の代わりに単一の sub-skill で済む場合。
