# Tasks — 動的ワークユニットモデル

**Task** は agent が作成・追跡・委譲・依存できる、永続的な第一級のワークユニットです。LLM が通常のツール呼び出しを通じてフロー**中に動的に**作成・管理します。従来の upfront *planner*（実行前に 2〜7 ステップの固定プランを宣言する単一ツール）の代替です。実行を始める前にプランを確定するのではなく、agent は進行しながら分解し、発見した依存関係を追加し、サブタスクを他のセッションに委譲します。

## なぜ upfront ではなく動的なのか

planner は LLM にオーケストレーションツールを事前選択させ、実行前に完全なプランを出力させました。これは弱いモデルがうまくこなせない形状であり、実行が実際の構造を明らかにしても適応できません。タスクモデルは**必要が生じたときに**LLM が使う小さな組み合わせ可能な ops を提供します（サブタスク作成、2 つのサブタスクの順序付け、1 つを完了にマーク、サブツリーの中断など）。採用はシステムプロンプトのルーティングガイド（「マルチターゲット / マルチステップ作業 → サブタスクに分解」）とカタログによって促進され、強制されたモードではありません。

## モデル

- **ワークユニットの識別子.** 各 Task は `task_id`、`name`、オプションの `description`、`status`（ライフサイクル: unassigned → ready → running → done / failed。依存関係が未解決の間は `blocked`、abort 時は `aborted`。`archived_at` は state ではなく Task レコード上の retention フィールドで、abort 時に設定される）を持ちます。
- **Requester と assignee.** **requester** は Task を作成したセッション（通知ターゲット）です。**assignee** は単一のワーカーセッションであり、Task のライフタイムを通じて**変更不可**です（引き継ぎなし）。`assignee` はデフォルトで呼び出し元（セルフタスク）になります。異なる値を指定するとクロスセッション委譲になります。
- **シングルライター CAS.** **assignee セッション**のみが Task のステータスを書き込めます。バックエンドの固定等価 compare-and-set（`assignee == caller session id`）で強制されます。呼び出し元セッション id は `OpContext.session_id` ルーティングキーであり、OS によってスレッドされます（op フィールドではありません）。終端状態の Task はそれ以降の書き込みをすべて拒否します（cooperative-terminal ガード）。トポロジーへの書き込み（依存関係、abort）は**requester**が所有します。
- **依存関係 DAG.** `deps` は depends-on エッジです。未解決の deps を持って生まれた Task は OS 派生の `blocked` になります。readiness は依存関係が完了するにつれ再計算されます（直接書き込まれることはありません）。エッジは存在確認とサイクルチェックが行われます。requester は依存タスクを代替に `repoint` できます（主なリカバリ手段）。
- **サブタスクのリンク種別.** decomposition child は `link_type`（`awaited` または `background`）を持ちます。`awaited` = 親がその子の結果を必要とする（完了をゲート）。`background` = 親は子と並行して処理を続け、その完了を待たない。RUNNING → DONE 遷移は `awaited` + `background` の open child が両方ゼロになった時のみ許可されます（completion-join ゲート）。

## Ops

11 の ops は、フェーズの control-IR からも、動的なワイヤリング以降はチャットルーターから `invoke_action`（`task__create`、`task__update_status`、…）経由でも呼び出せます。ルーターパスはフェーズパスと**同一の** assignee CAS を適用します。実際の呼び出し元セッション id をキーとし、バイパスはありません（ブリッジはゲートをマスクするようなセッションレスコンテキストでは実行を拒否します）。

| Op | ロールゲート | 目的 |
|---|---|---|
| `task.create` | requester = self | （サブ）タスクを作成。`deps` で順序付け、`assignee` で委譲。サブタスクの所有権は実行コンテキストから OS が派生 |
| `task.update_status` | **assignee**（CAS） | ステータス遷移を宣言（シングルライター） |
| `task.get` / `task.list` | — | 1 件取得 / 一覧（assignee / requester / status でフィルタ）。`requester=<task-id>` でそのタスクが所有するサブタスクを一覧 |
| `task.add_dependency` / `task.remove_dependency` | requester | depends-on エッジの追加 / 削除 |
| `task.repoint_dependency` | requester | エッジを代替にアトミックに張り替え（先にサイクルチェック） |
| `task.abort` | requester | Task とそのサブツリーを `aborted` に移行し `archived_at` を設定（cooperative-terminal、下方カスケード） |
| `task.heartbeat` | assignee | 生存確認と unblock-predicate 評価トリガー |
| `task.register_unblock_predicate` | assignee | 決定論的（LLM なし）unblock predicate を登録 |
| `task.comment` | — | Task のスレッドへ追記（agent 間 / ヒューマン・イン・ザ・ループ） |

ToolDefinition は IROp モデルから単一ソースで派生（`kind` ディスクリミネーターを除いた `model_json_schema()`）するため、LLM 向けスキーマがランタイムコントラクトからずれることはありません。

## 使いどころ

- **マルチターゲット / イテレーション**（「Y それぞれに X を行う」「N 件のファイルを処理する」）: ターゲットごとに 1 サブタスク + 残りに `deps` した最終集約タスク。
- **追跡価値のあるマルチステップ作業**: サブタスクを作成してステータスを更新し、ターンやクラッシュをまたいで進捗を永続化する。
- **委譲**: 別セッションを `assignee` にしたサブタスクを作成してピア agent に作業を渡す（ワーカーがそのステータスの唯一のライター）。

## 参照

- [Workspace](workspace.md) — フェーズ間で渡されるデータの単一信頼源
- [Events](events.md) — ランタイムのランごとの監査証跡
- [Permission model](permission-model.md) — ops が解決されるゲートレイヤー
