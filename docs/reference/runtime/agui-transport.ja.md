# AG-UI transport — thin-client wire protocol

Reyn の chat client は stream-consuming な UI です: セッションの出力を描画し、ユーザー入力をルーティングし、セッションには **transport seam を通じてのみ**触れます。この 1 つの seam の背後に 2 つの transport があります — ローカルの in-process transport と、この **AG-UI transport**(HTTP + Server-Sent Events / SSE 経由)です。両方とも同一のレンダラーにフィードするため、remote client はローカルと byte-for-byte 同じものを描画します。

このページは wire contract です: SSE エンドポイント、reyn-frame ⇄ AG-UI-event マッピング、`STATE_*` ステータス read-model。

## Surfaces

この transport は **AG-UI のみ** を話します — それは UI であり、agent ではありません。(agent↔agent は A2A、tool は MCP、observability export は OTEL — それぞれ別の surface です。)

- `GET /agui/chat/{agent}/events` — server→client の SSE ストリーム。各 SSE ブロックは `event: <TYPE>\ndata: <json>\n\n`。
- `POST /agui/chat/{agent}` — client→server のチャンネル。Body は JSON object で、現在のメッセージ type は `{"type": "user_message", "text": "..."}`(ターンの submit)。

両方とも server の authentication context でゲートされます: 接続は token を `?token=` または `Authorization: Bearer <token>` ヘッダーとして提示します(同一マシン上の UDS 接続は代わりに OS peer credential で識別されます)。未認証の接続は、どのセッションにも attach される前に `401` で拒否されます。

## Standard envelope, reyn-private richness

すべての event は **両方**を持ちます:

- **標準 AG-UI field shape** — 汎用の AG-UI client が相互運用可能なコア(text / tool / run / error / state)を描画できるようにするため。そして
- reyn-private な `_reyn` 再構築ブロック — reyn client がこれから正確な render frame を再構築します。

汎用 client は理解できないものを無視します: `_reyn` ブロックを持たない event(または汎用 client がモデル化しない reyn の `CUSTOM` event)は **スキップされる、致命的ではない** — reyn がこの ignore-unknown contract を所有します。

## Event mapping

client は 1 つの順序付き SSE ストリームを消費し、各 event をレンダラーの 2 つのエントリーポイント(display か working-indicator か)のいずれかにディスパッチします。マッピング:

### Display path(agent 出力 → scrollback)

| reyn display kind | AG-UI event        | 備考                                        |
|-------------------|--------------------|----------------------------------------------|
| `agent`           | `TEXT_MESSAGE_CONTENT` | assistant の返信テキスト                  |
| `status`          | `TEXT_MESSAGE_CONTENT` | 一時的なステータス行(`role: status`)    |
| `error`           | `RUN_ERROR`        | エラーテキスト                                   |
| `trace`           | `CUSTOM`           | reyn の tool/step トレース行                    |
| `intervention`    | `CUSTOM`           | プロンプトが表示される(回答の round-trip は後のフェーズ) |
| `presentation`    | `CUSTOM`           | `present` op の render-node モデル(*present-on-wire* 参照) |
| control sentinel  | `CUSTOM`           | `__end__` と client-local な control kind     |

この表にない display kind もすべて losslessly round-trip します(`CUSTOM` にフォールバックし `_reyn` から再構築される)— 新しいレンダラー kind が wire 上で黙って消えることはありません。

### Working-indicator path(ターンライフサイクル + tool 軸)

| reyn chat-event               | AG-UI event      |
|-------------------------------|------------------|
| `turn_started`                | `RUN_STARTED`    |
| `turn_settled` / `turn_completed` / `turn_cancelled` | `RUN_FINISHED` |
| `tool_called`                 | `TOOL_CALL_START`|
| `tool_returned` / `tool_failed` | `TOOL_CALL_END`|
| `user_answered_intervention`  | `CUSTOM`         |

この 8 つが、レンダラーの working / running / waiting-for-you インジケーターが消費する正確なセットです — transport はこのセットをそのまま転送します。

## present-on-wire

`present` op の render モデルは render node の `list[dict]` であり、**構築時に neutralize** されています(すべての leaf string からターミナル制御 / ESC シーケンスが除去済み)— そのため wire に到達する前に inert です。これは `presentation` display kind の下で `CUSTOM` event に乗り、`meta.nodes` に格納されます。

AG-UI client はさらに、**transport edge で**、接続ごとに、すべての node leaf に対して surface neutralizer を再実行します — 構築 seam が既に neutralize した leaf に対しては冪等ですが、upstream が neutralize しなかった(または別の surface 用に neutralize した)heterogeneous-surface client にとっては load-bearing な defense-in-depth です。

## STATE_* — ステータス read-model

ステータスバー(attached agent、model、cost、token、context 使用量、現在の WaitingOn ラベル)は **read-model** であり、ファイルミラーではありません: セッションの live な cost / token / context アクセサと working-indicator の状態から導出され、render に関連するサブセットのみがストリームされます。

- `STATE_SNAPSHOT` — **接続時**に発行される、完全な read-model。フィールド: `attached_name`、`model`、`cost_agent`、`cost_total`、`agent_tokens`、`ctx_used`、`ctx_window`、`waiting_on`。
- `STATE_DELTA` — **変更時**に発行される、変更されたキーのみを運ぶ。アイドルなストリームは delta を発行しません。

client はスナップショットから自身のステータスビューを seed し、各 delta をマージするため、remote のステータスパネルは常に server の値を反映します。

## Reconnect

接続(または再接続)時、server は以下を、どのライブ event よりも前に再生します:

1. `MESSAGES_SNAPSHOT` — display のバックログ(既に生成されたメッセージ)。再接続する client が自身の scrollback を再構築できるようにする。それから
2. `STATE_SNAPSHOT` — 上記のステータス read-model。

ライブ event(と `STATE_DELTA`)がそれに続きます。

## Local ≡ remote

server はローカルの in-process transport が生成するのと**同じ**統一 frame stream(display outbox + レンダラーに関連する chat-event のサブセット)をシリアライズします。AG-UI transport が加えるのは wire framing のみで、新しい render semantics は一切加えません — そのため remote レンダラーの display バイトと working-indicator の遷移は、ローカルのものと同一です。

## AG-UI event coverage — 数字を正直に読む

**以下の数字にかかわらず、frame loss はゼロ、reyn-client の fidelity は 100% です。** すべての event は reyn-private な `_reyn` 再構築ブロックを運びます(上記の *Standard envelope, reyn-private richness* 参照)。reyn client は常にこれから正確な元の frame を復元します。このセクションの coverage の数字が記述しているのは別のことです: **AG-UI の *標準*イベント語彙のうちどれだけを reyn がネイティブに発行しているか**(= reyn の知識なしに描画できる、汎用の非-reyn AG-UI client が見る信号)であり、`CUSTOM` event に折りたたまれ汎用 client がスキップせざるを得ないものとの対比です。ここでの低い数字は、汎用-client の richness についての記述であり、data loss についての記述ではありません。

| Category   | 標準 event 数 | reyn-mapped | Disposition |
|------------|-----------------|-------------|--------------|
| State      | 3                | 3           | **complete** |
| Lifecycle  | 5                | 3           | **intentional-scope** — 2 つの Step event は独立した標準 event としてではなく `STATE_*` read-model の `waiting_on` フィールドに fold される(上記 *STATE_\* — ステータス read-model* 参照) |
| Tool       | 5                | 2(→3 予定) | `TOOL_CALL_RESULT` は **next-phase**(後のフェーズの HITL frontend-tool 回答 round-trip で追加される); `TOOL_CALL_ARGS`/`_CHUNK` のペアは **intentional-scope**(reyn が発行する時点で tool call は既に完了しており、chunk 化すべき in-flight な args ストリームが存在しない) |
| Text       | 4                | 1           | **intentional-scope** — reyn の outbox はトークン差分ではなく whole message を配信するため、メッセージごとに 1 つの `TEXT_MESSAGE_CONTENT` が正直なマッピングです。マップすべき `_START`/`_END`/streaming-chunk フェーズは存在しません |
| Special    | 2                | 1           | **intentional-scope** — reyn-private なペイロードは常に構造化されている(`CUSTOM`)。標準の `RAW` passthrough event に reyn の use case はありません |
| Activity   | 2                | 0           | **intentional-scope** — reyn に直接の analog がありません。同じ情報は既に frame stream + `STATE_*` が運んでいます |
| Reasoning  | 7                | 0           | **future-candidate** — 最も価値の高いギャップ(下記参照) |

**合計**: reyn は active-roster の標準 event **28 件中 9 件**をネイティブに発行しています(`CUSTOM` catch-all 自体を 1 件と数えると 10/28)。この 28 件の roster は Lifecycle(5)+ Text(4)+ Tool(5)+ State(3)+ Activity(2)+ Reasoning(7)+ Special(2)で、canonical な AG-UI event reference(<https://docs.ag-ui.com/concepts/events>)から集計しています。この reference は、active roster 外の meta/deprecated/draft entry を含めると最大で ~34 件の event 名を自称しています — 正確な数字は spec version に依存するため、このページは(より大きい数字ではなく)28 件の active roster を追跡対象としています。

### なぜこのようにギャップが disposition されているか

- **Reasoning(future-candidate、最高価値)。** reyn は既に reasoning を first-class な概念として扱っています。現在、reasoning トレースは `trace` display kind に乗り `CUSTOM` になるため、汎用 client には見えません。これを標準の `Reasoning*` event にマッピングすれば、汎用 AG-UI client がそれを直接描画できるようになります。この機能を出荷する前に尊重しなければならないゲート: reyn の **reasoning-display トグル** — operator が reasoning display を off にしている場合、wire 上にも何も発行されるべきではありません。マッピングがそのトグルを迂回する chain-of-thought 露出経路になってはいけません。
- **Tool result の fidelity(non-blocking、低コスト)。** 汎用 client は現在、`tool_failed` と `tool_returned` を区別できません — どちらも標準の `TOOL_CALL_END` event に collapse し、失敗の事実は `_reyn`(汎用 client がスキップする)からしか復元できません。reyn-client の fidelity には影響しません。将来のパスで、汎用-client の可視性のために標準の `TOOL_CALL_END` ペイロード自体に error/status フィールドを表面化させることができ、実装コストは低いです。
- **intentional-scope とマークされたものはすべて**、見落としではなく本物のアーキテクチャ上の違い(reyn の whole-message outbox、構造化のみの private ペイロード、in-flight な tool-args フェーズが無いこと、直接の "activity" 概念が無いこと)を反映しています — これらのギャップを埋めることは、バグを直すことではなく、reyn の設計が意図的に持っていない streaming/chunking の機構を発明することを意味します。
