# AG-UI transport — シンクライアントのワイヤープロトコル

Reyn のチャットクライアントはストリームを消費する UI である — セッションの出力を描画し、
ユーザー入力をルーティングし、セッションには**transport seam を通じてのみ**接触する。この
1 つの seam の背後には 2 つの transport がある — ローカルの in-process transport と、この
**AG-UI transport**(HTTP + Server-Sent Events / SSE 経由)だ。両方とも同一のレンダラーへ
フィードするため、リモートクライアントはローカルのものと byte-for-byte 同一の描画を行う。

このページは wire contract そのものである。SSE エンドポイント、reyn-frame ⇄ AG-UI-event の
マッピング、そして `STATE_*` ステータス read-model を扱う。

## Surfaces

この transport が話すのは **AG-UI のみ**である — これは UI であり、agent ではない
(agent↔agent は A2A、tool は MCP、observability export は OTEL — それぞれ別の surface で
ある)。

- `GET /agui/chat/{agent}/events` — server→client の SSE ストリーム。各 SSE ブロックは
  `event: <TYPE>\ndata: <json>\n\n` である。
- `POST /agui/chat/{agent}` — client→server のチャンネル。Body は JSON object で、サポート
  されるメッセージ type は以下の通り:
  - `{"type": "user_message", "text": "..."}` — ターンを submit する。レスポンス body は
    `{"status": "ok", "msg_id": "..."}`(#3287)——broadcast される
    `reyn.event.user_submitted` が運ぶのと同じ correlation id であり、送信した
    クライアント自身がこの id で自分の echo を認識できる(下記「Local ≡ remote は
    input についても」参照)。
  - `{"type": "TOOL_CALL_RESULT", "toolCallId": "<intervention-id>", "text": "..."}` または
    `{..., "choiceId": "<id>"}` — pending 中の intervention に回答する(HITL の round-trip;
    下記「Human-in-the-loop answering」参照)。
  - `{"type": "cancel_inflight"}` — in-flight なターンを協調的にキャンセルする(Ctrl-C
    seam)。
  - `{"type": "cancel_queued", "msg_id": "..."}` — まだ dispatch されていない(未実行の)
    inbox メッセージを id 指定でキャンセルする(#3300 P3)。上記の `cancel_inflight` とは
    異なる意図である — こちらは「まだ turn が始まっていない特定の queued item」を対象と
    し、現在実行中の turn は対象にしない。サーバー側の意味論(`Session.cancel_queued`):
    queued の場合 → 除去(WAL `inbox_cancel` tombstone + 同期的な snapshot-prune の後、
    `inbox_cancel` audit-event delta を emit。下記「STATE_* — ステータス read-model」と
    `reyn.event.inbox_cancel` を参照)。既に dispatch 済みの場合 → no-op(`cancel_inflight`
    へのエスカレーションは行わない)。冪等(同じ id への2回目のキャンセルは no-op — at-most-once
    の再送に対して安全)。
  - `{"type": "heartbeat"}` — liveness の keepalive。

  server がモデル化していない入力 type は**グレースフルな no-op**(`200` の ack)であり、
  `500` にはならない — これが server 側の ignore-unknown である。
- `POST /agui/chat/{agent}/seize` — active-driver トークンを取得する(「Active driver and
  seize」参照)。

`POST /agui/chat/{agent}` と `POST /agui/chat/{agent}/seize`(#5129)の `{agent}`
は**fallback であり宛先ではない**: 両ルートとも、実際のターゲットはこの接続自身の
`connection_id`(現在アタッチしているエージェント)から解決される — `{agent}` が参照
されるのは、その接続に紐づくアタッチ記録がまだ無い場合のみ。したがって `--connect`
した client 自身のクロスエージェント `/attach` は、client が POST 先の URL を変更
しなくても、その次の `submit`/`seize` をリダイレクトする。

client が server をシャットダウンすることは決してできない — shutdown メッセージは存在せず、
client の `/quit` はローカルな切断にすぎない。server が唯一の writer である。

connection は `connection_id` クエリパラメータ(または `X-Reyn-Connection` ヘッダー)で自身を
識別し、その値は SSE ストリームと POST を通じて安定している。

両方とも server の authentication context によってゲートされる: connection は token を
`?token=` または `Authorization: Bearer <token>` ヘッダーとして提示する(同一マシン上の UDS
connection は代わりに OS の peer credential で識別される)。未認証の connection は、どの
session にも attach される前に `401` で拒否される。この transport を開く operator 向けの
コマンドは `reyn chat --connect <url>`(bearer token 用の `--token <secret>`、フォールバック
として `REYN_WEB_AUTH_TOKEN` 環境変数)である。

## Standard envelope, reyn-private richness

すべての event は**両方**を運ぶ:

- **標準的な AG-UI field shape** — 汎用の AG-UI client が相互運用可能なコア(text / tool /
  run / error / state)を描画できるようにするため。そして
- reyn-private な `_reyn` 再構成ブロック — reyn client がこれから正確な render frame を
  再構築する。

汎用 client は理解できないものを無視する: `_reyn` ブロックを持たない event(または汎用
client がモデル化しない reyn の `CUSTOM` event)は**スキップされる、致命的ではない** —
reyn がこの ignore-unknown contract を所有する。

## Event mapping

client は 1 つの順序付き SSE ストリームを消費し、各 event をレンダラーの 2 つのエントリー
ポイント(display か working-indicator か)のいずれかにディスパッチする。マッピングは
以下の通り。

### Display path(agent 出力 → scrollback)

| reyn display kind | AG-UI event        | Notes                                        |
|-------------------|--------------------|----------------------------------------------|
| `agent`           | text triplet       | assistant の返信テキスト(*text lifecycle* 参照) |
| `status`          | text triplet       | 一時的なステータス行(`role: status`)        |
| `reasoning`       | reasoning triplet  | モデルの reasoning テキスト(*reasoning lifecycle* 参照)。reasoning display が on のときのみ emit される |
| `error`           | `RUN_ERROR`        | エラーテキスト                                |
| `intervention`    | `CUSTOM`           | プロンプトが表示される。reyn client はそれをネイティブに描画し、id で回答する(「Human-in-the-loop answering」参照) |
| `presentation`    | `CUSTOM`           | `present` op の render-node モデル(*present-on-wire* 参照) |
| `__copy_last_reply__` / `__rewind_list__` | `CUSTOM` | クライアント消費センチネル — 転送される(*control sentinels* 参照) |
| `__end__` | *(フィルタ)* | 転送されない(*control sentinels* 参照) |

これら以外の display kind もすべて損失なく round-trip する(`CUSTOM` にフォールバックし
`_reyn` から再構成される)— 新しい display kind がワイヤー上で静かに消えることは決してない。
これを保証する completeness gate は、**権威あるプロデューサードメイン** — ソース全体の各
`OutboxMessage(kind=...)` リテラル(直接構築 + kind フォワーダーヘルパーの呼び出し箇所)であり、
renderer ファイルの proxy ではない — を列挙し、各プロデューサー kind が *standard-mapped* /
*profiled* / *control-filtered* のいずれかであることをアサートする。それ以外は CI で失敗する。

#### Control sentinels(転送 vs フィルタ)

いくつかの `__…__` display kind は **per-entry の disposition** を持ち、それは *センチネルが
どこで消費されるか* で決まる(forward-set の否定では決してない。否定すると描画可能な
display kind を誤って落としてしまう):

- **クライアント消費 → 転送**(profiled `CUSTOM`、`_reyn` ロスレス):
  - `__copy_last_reply__` — `/copy`: **クライアント** が transport ストリーム越しに実際の
    クライアント側クリップボードコピーを行う。
  - `__rewind_list__` — `/rewind`: **クライアント** が rewind 領域ピッカーを描画する。

  thin-client モデルでは transport が AG-UI ワイヤーそのものなので、これらをフィルタすると
  リモートの `/copy` / `/rewind` が silent no-op になる — ワイヤーに届く必要がある。
- **フィルタ**(`CONTROL_FILTER_KINDS`、明示的 allowlist — emitter はワイヤーイベントを出さない):
  - `__end__` — ストリーム終端(emitter はこれで return する。クライアントのループもストリーム
    クローズで終わる)。
  - `__open_artifact__` — 構造上ローカル専用(クライアントが動くマシン上で OS アプリを
    起動する;*#4482* 参照)。
- **廃止済み**(#4534 PR-2 / PR-2b): `__attach_request__` と `__session_switch_request__`
  はもう存在しない。`/agent new`・`/attach`・`/session switch` はすべて今や named
  operation である `ClientTransport.request_attach` / `request_session_switch` を経由する
  (display channel のセンチネルではない — #3595 S5 の原則「client が解釈し、server が
  名前つき操作を実行する」を `run_slash_command` と同じ形で適用)。本 doc の以前の版は
  `__attach_request__` を「転送され、実際に live」、`__session_switch_request__` を
  「tap が消費しフィルタされる」と記述していたが、それぞれ各 PR 着地前は正確だった。
  セッション switch-follow(下記)ももう outbox からセンチネルを消費せず、
  `registry.add_attach_listener` に直接 subscribe する。

#### Text lifecycle(適合する triplet — plain と streamed)

AG-UI 仕様は、text lifecycle として**`TEXT_MESSAGE_START` → 1 つ以上の
`TEXT_MESSAGE_CONTENT` → `TEXT_MESSAGE_END`、すべて `messageId` で相関付けられる**ことを
必須としている。裸の `TEXT_MESSAGE_CONTENT` は不正である(厳格な汎用クライアントはそれを
破棄する)。

streaming が適用されるのは**narrative reply 経路のみ**である。`RouterLoop`
から `call_llm_tools` を呼ぶプロダクション経路は 3 つあるが、
`on_content_delta` を渡すのは primary reply(`run_loop`)だけである。
**structured-answer turn(`_run_structured_answer_turn`)は意図的に
非-streaming**——出力を `json.loads` でパースする schema 制約付きターンで
あるため、部分 JSON を stream しても解析不能で無意味である。**force-close
の wrap-up 呼び出し(`_force_close_call`)も非-streaming**である(本文では
なく終端の締めであるため)。

**一度もストリーミングしなかったメッセージ**(provider capability なし、ADR-0039 P3a/③a)
は、この triplet に plain な whole-message としてワイヤーに乗る——メッセージごとに生成
される id を伴い(reyn の outbox には安定した message id がない)、単一の CONTENT の
`delta` がメッセージ全文を運ぶ。`_reyn` 再構成ブロックを運ぶのは **CONTENT** イベントのみ
であり、START/END は汎用のスキャフォールドで reyn client はそれらを `None` にデコードして
無視する——再構成の invariant は**1 フレーム ⇄ 1 つの `_reyn` 保持イベント**である。

**実際にストリーミングしたメッセージ**(#3288 ③b が raw な LLM の content-delta を届く
たびに `reyn.event.agent_delta` を emit し、#3288 ③d がそれを標準の text surface に
マップする)は代わりに**本物の multi-CONTENT シーケンス**を得る: 最初の delta で
`TEXT_MESSAGE_START`、delta ごとに 1 つの本物の `TEXT_MESSAGE_CONTENT`(それぞれが自分
自身の `_reyn` を運び、その正確な `agent_delta` audit-event を再構成する——そのため reyn
client のインフライトな描画は、フレームが in-process で届いたのかこのワイヤー経由で届いた
のかによらず同一である)、そして完了時に `TEXT_MESSAGE_END`。★完了は **END のみ**に
マップされる——全文を再送する 2 つ目の CONTENT は**決して**発行しない(delta をライブ描画
していた client がボディを二重描画してしまうため)。END は代わりに自分自身の `_reyn` を
運び、その完了時点での永続化済み全文を保持する——これがストリーミングされたメッセージの
**唯一の再構成 authority** である。reyn client は delta を連結して再構成することは決して
ない(delta は非永続的で、派生的な、live-only の narration にすぎない——再接続時の
`MESSAGES_SNAPSHOT` backlog は永続化された `OutboxMessage` からのみ構築されるため、delta
を読むこともない)。これは**late-joiner window** を閉じるものでもある: 各 connection ごと
の `TextStreamTracker` 状態(`interfaces/transport/agui/emitter.py`)はその connection が
実際に観測した delta のみを反映する——ある chain の delta を一つも観測しなかった
connection(ストリーム中に接続していなかった、あるいは終了直前に接続した)は、同じ完了
フレームに対して代わりに変更されない plain な whole-message triplet(全文が CONTENT に
乗る)を得る——いずれにせよ client は完全な永続化済みテキストに到達する。「どの delta を
受け取ったか」というクライアントごとの bookkeeping は一切保持されない(issue #3288 ③d の
設計スレッドで却下——権威ある完了を読むことに対して利益なくして状態を追加するだけになる)。

つまり再構成の invariant は**ストリーミングされたメッセージについてのみ再決定される**:
N 個の `agent_delta` CONTENT イベントはそれぞれ自分自身の `_reyn`(delta ごとに 1 つ)を
運び、終端の END はさらに別の、DISTINCT な `_reyn`(完了)を運ぶ。1 つではなく N+1 個の
`_reyn` 保持イベントだが、client の再構成 authority は常に最後の 1 つである。一度もストリ
ーミングしなかったメッセージはこれらの影響を受けない(上記の plain な triplet のまま
変わらない)。

#### Reasoning lifecycle(適合する triplet)

reyn のモデル reasoning は AG-UI の **Reasoning** メッセージ lifecycle に乗り、汎用
クライアントがそれを不透明な `CUSTOM` ペイロードではなく reasoning として描画できるように
する。canonical な Reasoning カテゴリは 7 つの event を持つが、reyn は whole-message
(トークンストリーミングなし)であるため、コンテンツを運ぶ内側の triplet
**`REASONING_MESSAGE_START` → `REASONING_MESSAGE_CONTENT` → `REASONING_MESSAGE_END`、
`messageId` で相関付け**を、`role: "reasoning"` と全文を運ぶ CONTENT の `delta` とともに
マップする。これは text triplet を正確にミラーする。`_reyn` ブロックを運ぶのは CONTENT
イベントのみ(START/END は `None` にデコードされる)であり、reyn client はちょうど 1 つの
reasoning display frame を再構築し、その描画はバイト単位で不変である。

この signal を保つ 2 つの境界:

- **構成による display ゲート。** reasoning display frame は operator の
  reasoning-display トグルが on のときのみ存在する — reyn はそのトグルでゲートされた単一の
  chokepoint でフレームを emit する。display が off ⇒ reasoning frame なし ⇒ ワイヤー上に
  `REASONING_*` event はゼロ。マッピングは新しいゲートを追加せず、トグルを迂回する
  chain-of-thought 露出経路にはなり得ない。
- **reasoning は display signal であり observability ではない。** AG-UI の display 面は
  operator の接続クライアントであり、display-on は「見る意図」である。reasoning コンテンツは
  transport-frame の関心事であり、observability export には決してルーティングされない —
  OTLP exporter は content-off デフォルトを保ち、reasoning chain-of-thought を一切受け取らない。

### Working-indicator path(turn lifecycle + tool 軸)

| reyn audit-event              | AG-UI event      |
|-------------------------------|------------------|
| `turn_started`                | `RUN_STARTED`    |
| `turn_settled` / `turn_completed` / `turn_cancelled` | `RUN_FINISHED` |
| `tool_called`                 | `TOOL_CALL_START`|
| `tool_returned` / `tool_failed` | `TOOL_CALL_END` (with `status`) |
| `user_answered_intervention`  | `CUSTOM`         |

これらの 8 つが、レンダラーの working / running / waiting-for-you インジケーターが消費する
正確なセットである — transport はこのセットをそのまま転送する。

`TOOL_CALL_END` は etype から導出された標準の `status` フィールド(`"ok"` / `"error"`)を
運ぶ — `tool_failed` → `"error"`、`tool_returned` → `"ok"` — これにより汎用クライアントも
ツールの失敗を認識できる。reyn client は依然として `_reyn` から正確な etype を
exact-recover する。

### Intervention frontend-tool

display frame と並行して、server は `toolName` が `reyn.intervention.<kind>`、`toolCallId`
が intervention id であるような、対になる `TOOL_CALL_START` **frontend-tool** を発行する。
汎用 AG-UI client はこれを通常の tool call として描画・回答できる。reyn client はこれを、
どの intervention が pending かを知るためだけに使う — プロンプト自体は display frame から
自ら描画するため、二重描画は発生しない。intervention が解決(回答または拒否)されると、
server は終端の `TOOL_CALL_RESULT` を発行するため、pending の frontend-tool が宙に浮くこと
はない。

## Human-in-the-loop answering

intervention への回答は permission grant **そのもの**であり、すべての回答は配信時に認証
**かつ**認可される。client は信頼されない: server は identity を再認可し、回答を
intervention 自身の**自前のコピー**(id、および choice id があればそれ)に照らして検証する
— client がエコーする prompt / choices は信頼されない。

回答は**id によって**配信される: `TOOL_CALL_RESULT` の `toolCallId` は operator に提示され
た正確な intervention を指定するため、grant はその prompt に着地し、別のキュー中の
intervention に着地することは決してない。未知の id、または既に回答済みの id は拒否される
(client は通常のターンにフォールバックする)— 最も古いものに回答するというフォールバック
は存在しない。

認証済みの人間の operator による回答は unfenced である(信頼された operator 入力として
扱われる)。internal な agent-to-agent path 経由で外部の agent peer から届く回答は fenced
のままである(異なる、信頼されない trust class)。

Attribution: 回答済みの各 grant は、認証済みの user id とその発信元 connection とともに
audit trail に記録される。attach / seize / detach も同様に監査される。

## Active driver and seize

複数の terminal が 1 つの session に attach でき、すべてが同じ出力を見る。ある時点で厳密に
1 つの connection だけが **active-driver token**(回答/操作する権限)を保持する。これは
UX 上の調整トークンであり、security control ではない。

認可された任意の connection は、handshake なしにトークンを**seize**(`POST
/agui/chat/{agent}/seize`)できる — 想定されるケースは、1 人の operator がノート PC と
デスクトップを行き来する場合である。以前の保持者は保持しない対等な peer となり、seize し
返すこともできる。

seize は、未認証 / 未認可の connection、または attach された surface を持たない connection
に対しては拒否される。地位を追われた保持者の in-flight な回答は配信時に拒否される(もはや
active driver ではないため)。

## Fail-close and the grace window

pending の intervention が、いなくなった operator を待って永遠にハングすることは決してあっ
てはならない。ある intervention に対する最後の回答可能な operator surface が失われたとき
— in-process な detach、または network の切断 / heartbeat タイムアウトのいずれか — その
intervention は型付けされた拒否(run がそこから継続する fail-closed な回答)で解決され、
放置されることはない。

これが起きるのは**grace window**を経過した後のみである: window 内での短い切断と再接続は
intervention を pending のまま保ち、正常に再開する。surface がゼロのまま grace window を
丸ごと経過した場合にのみ拒否がトリガーされる。

liveness signal(定期的な heartbeat)により、half-open な connection が死んだ surface を
隠すことはできない: liveness タイムアウトを超えて heartbeat を止めた surface は失われたと
検出される。

heartbeat POST は**half-open の backstop に過ぎない** — 通常の切断(client が cleanly に
close する場合)は SSE handler 自身の `finally: manager.detach(...)` により即座に検出され、
heartbeat には依存しない。専用の ping が意味を持つのは、TCP FIN を一切送らずに hang した
client のケースのみである。remote thin client(`reyn chat --connect`)は 25s ごとに heartbeat
を送信し(`REYN_AGUI_HEARTBEAT_INTERVAL_S` で override 可能)、その window 内で実際の
client→server POST(turn / answer / cancel)が既に届いていれば専用 ping を skip する
(piggyback — 実トラフィックに便乗し、冗長な負荷を避ける)。server 側の liveness タイムアウト
は 60s(`REYN_AGUI_LIVENESS_TIMEOUT_S` で override 可能)— client の interval に対して十分な
余裕を持つ(業界標準の比率: Socket.IO 25s/60s、Phoenix 30s、SignalR 15s+2×timeout)ため、
live だが idle な client が誤って swept されることはない。client の interval は必ず server の
timeout を下回り、timeout はさらに timeout+grace を下回る必要があり、これにより half-open
backstop と grace window が合わせて検出をカバーし続ける。

拒否は**intervention ごと**に scope される: 別の live な surface(例えば外部の agent peer
が回答しているもの)がまだ回答可能な intervention は、operator の terminal がすべていなく
なっても pending のまま残される。

## present-on-wire

`present` op の render モデルは render node の `list[dict]` であり、**構築時に neutralize**
されている(すべての leaf 文字列からターミナル制御 / ESC シーケンスが除去済み)ため、どの
wire に到達する前にも inert である。これは `presentation` display kind の下で `CUSTOM`
event に乗り、`meta.nodes` に格納される。

AG-UI client はさらに、**transport edge で**、接続ごとに、すべての node leaf に対して
surface neutralizer を再実行する — 構築 seam が既に neutralize した leaf に対しては冪等だ
が、upstream が neutralize しなかった(あるいは別の surface 用に neutralize した)
heterogeneous-surface client にとっては load-bearing な defense-in-depth である。

## STATE_* — ステータス read-model

ステータスバー(attached agent、model、cost、tokens、context usage、そして現在の
WaitingOn ラベル)は**read-model**であり、ファイルミラーではない: これはセッションの生き
た cost / token / context アクセサと working-indicator の状態から導出され、render に関連
する部分集合のみがストリームされる。

- `STATE_SNAPSHOT` — **接続時**に発行される、read-model 全体。フィールド: `attached_name`、
  `model`、`cost_agent`、`cost_total`、`agent_tokens`、`ctx_used`、`ctx_window`、
  `waiting_on`、`queue`、`turn_active`、`halted_reason`。
- `STATE_DELTA` — **変更時**に発行され、変更されたキーのみを運ぶ。アイドルなストリームは
  delta を発行しない。

`halted_reason`(#2280)は `Session.halted_reason` —実行中は `None`、永続的な
durability failure(#2259)でセッションが fail-stop した後は理由(例:
`"durability_failure"`)。同じ snapshot+delta channel に乗せることで、remote client
もローカルの TUI status line / plain `--cui` bottom toolbar と同じ proactive な表示を
得る — halt 自体は既に別の場所(`DurabilityHaltError`)で同期的に enforce されており、
このフィールドは observability 専用で halt 自体には load-bearing ではない。

`queue` と `turn_active`(#3300 P2a)は、サーバー権威の **sent-queue 状態**を publish する:
`queue` は現在未 dispatch の inbox キュー(各 item は `{msg_id, chain_id, text}` —
`Session.queued_user_messages()`)、`turn_active` は turn が現在 dispatch 中かどうか
(`Session.turn_active`)。同じ snapshot+delta channel に乗せることで、client は
**late-joiner-safe** になる: turn 途中で接続した(dispatch を引き起こした
`turn_started` audit-event を見逃した)client も、部分的な event 由来の推測ではなく
snapshot から正しい queue + turn-active 状態を得る。P2a はこの状態の publish のみで、
sent-queue widget としての描画は P2b。

item が `queue` から抜けるのは、同じ snapshot+delta channel 上の互いに排他な2つの
granular audit-event delta のいずれかを経由する — `turn_started`(dispatch された。下記
「Working-indicator path」参照)、または `inbox_cancel`(上記の `cancel_queued` client
message で id 指定キャンセルされた。#3300 P3)。server 自身の atomic な
queued/dispatched 判定が、ある item についてこの2つのうち正確に一方のみが必ず発火する
ことを保証する(両方が発火することはない)。`inbox_cancel` は `msg_id` と `seq`(
`user_submitted`/`turn_started` と同じ order-race-gate token — 下記
`reyn.event.inbox_cancel` を参照)を運ぶ。granular delta をマージする client は
(`turn_started` が `chain_id` でマッチするのとは異なり)`msg_id` で item を除去する。

client は snapshot から自身のステータスビューを seed し、各 delta をマージするため、
remote のステータスパネルは常に server の値を反映する。

## Reconnect

接続(または再接続)時、server はどのライブ event よりも前に、以下を replay する:

1. `MESSAGES_SNAPSHOT` — display のバックログ(既に生成されたメッセージ)。再接続する
   client が自身の scrollback を再構築できるようにする。続いて
2. `STATE_SNAPSHOT` — 上記のステータス read-model。

その後にライブ event(および `STATE_DELTA`)が続く。

`MESSAGES_SNAPSHOT` の `messages` フィールドは、会話ターンのみからなる標準的な
`[{role, content}]` **配列**である — `agent` → `assistant`、`user` → `user` — これは汎用
client が期待する形状である。reyn の chrome(status / error / present / intervention /
trace)は会話ターンではないため、この標準配列からは除外される。reyn client は `_reyn`
ブロックからバックログ全体(chrome を含む)を再構築するため、その scrollback は変わらない。

## The reyn extension profile

相互運用可能なコアを超えて、reyn は reyn 所有の namespace の下に自分自身の語彙を名付ける
— 標準的な対応物を持たない chrome のための `CUSTOM`-event `name`、そして intervention の
ための frontend-tool `toolName` である。この namespace は**文書化され、テストされた拡張
プロファイル**である: reyn が発行するすべての `reyn.*` name はレジストリエントリを持つ。
completeness gate が、**権威あるプロデューサードメイン** — ソース全体の各
`OutboxMessage(kind=...)` リテラル(直接構築 + kind フォワーダーヘルパーの呼び出し箇所)であり、
renderer ファイルの proxy ではない — に加えて intervention frontend-tool エンコーダーを列挙し、
各プロデューサー kind が *standard-mapped* / *profiled* / *control-filtered* のいずれかであることを
assert するため、このプロファイルは codec がワイヤーに乗せるものから静かにドリフトすることがない。

3 つの namespace がある:

### `reyn.display.<kind>`

標準的な AG-UI 対応物を持たない reyn の display frame。`value` は `{"text": <string>}` —
display 行のテキストである。

| Custom `name`                     | Meaning                                              |
|-----------------------------------|------------------------------------------------------|
| `reyn.display.intervention`       | intervention プロンプトが表示される                     |
| `reyn.display.presentation`       | `present` op のテキスト。render-node モデルは `_reyn` ブロックの `meta.nodes` に乗る(ワイヤー上は inert — *present-on-wire* 参照) |
| `reyn.display.user`               | user-authored な行 — 送信されたターン、または解決された intervention への回答。`reyn.event.user_submitted` / `reyn.event.intervention_answer_submitted`(下記)を受けた各 surface がローカルに描画したものであり、#3300 以降どの producer も `session.outbox` へ PUT しない(最後にそれをしていた site = intervention-answer echo も event 化された)。`OutboxMessage` の有効な kind としては残る(surface 自身のローカル構築 — 例えば永続化済み transcript の restore — 用、および fail-safe な profile entry として)が、outbox fan-out で流れる live な wire kind ではない。`meta` はマルチクライアント描画向けに `auth_user_id` / `auth_connection_id` の attribution を任意で運ぶ(backlog の user ターンは代わりに標準の `messages` 配列に乗る) |
| `reyn.display.system`             | reyn chrome 行 — 永続化されるライフサイクル/ステータスマーカー(compaction / budget / cost-warn) |
| `reyn.display.__copy_last_reply__` | `/copy` センチネル — 転送される(クライアント側クリップボードコピー);*control sentinels* 参照 |
| `reyn.display.__rewind_list__`    | `/rewind` センチネル — 転送される(クライアント側 rewind ピッカー);*control sentinels* 参照 |
| `reyn.display.tool_call_started`  | tool-call 開始のトレース行                              |
| `reyn.display.tool_call_completed`| tool-call 完了のトレース行                              |
| `reyn.display.tool_call_failed`   | tool-call 失敗のトレース行                              |

### `reyn.event.<etype>`

標準的な AG-UI 対応物を持たない reyn の audit-event。`value` はそのイベントのデータ
オブジェクトである。ほとんどのメンバーは working-indicator 軸(turn-lifecycle /
tool-call / user-submitted / cancel)だが、`agent_delta`(#3288 ③b)は**別系統の
streaming-notification 軸**である——下記の行、および `frames.py` の
`_STREAMING_EVENTS` コメント(なぜレンダラー consumer に先行して forward されたか)を
参照。plain/repl レンダラーは今も `agent_delta` の分岐を持たない(将来も持たない可能性が
ある)。Textual TUI(`interfaces/inline/textual_chat`)が #3288 ③c 時点でこれを消費する
唯一の surface である。★この `CUSTOM` マッピングは(plain な非ストリーミング codec
経路である)`encode_frame`/`encode_frame_wire` が `agent_delta` の `EventFrame` に対して
生成するものである——AG-UI emitter の実際の本番呼び出し箇所(`emitter.py`)は代わりに
すべてのフレームを `encode_frame_wire_streaming` に通し、`agent_delta` を(この `CUSTOM`
name ではなく)標準の `TEXT_MESSAGE_CONTENT` surface にマップする(#3288 ③d — 上記
*Text lifecycle* 参照)——実際に client が受け取るワイヤー上ではこの `CUSTOM` name は
現れない。この行が記述しているのは plain codec 関数が単体で行うこと
(`tests/interfaces/test_agent_delta_audit_event_3288.py` が直接検証する)であり、接続済みの
ワイヤー上で起きることではない。

| Custom `name`                        | Meaning                                          |
|--------------------------------------|--------------------------------------------------|
| `reyn.event.user_answered_intervention` | ユーザーが intervention に回答した(working-indicator 軸のみ — display text は運ばない;echo は下記の `reyn.event.intervention_answer_submitted` を参照) |
| `reyn.event.user_submitted`          | ユーザーがターンを送信した(#3300 P1 C)— RAW text + chain_id + msg_id + seq + meta を運ぶ。各 surface がそれぞれの render 境界で neutralize する。`msg_id`/`seq` は #3300 P2a の sent-queue correlation id + order-race-gate token |
| `reyn.event.intervention_answer_submitted` | intervention への回答が解決した(#3300 — 最後に残っていた outbox `kind="user"` ブロードキャスト site である `InterventionHandler.deliver_answer_to` を event 化)— RAW text(生の回答、またはマッチした choice の label)+ `intervention_id` + meta を運ぶ。各 surface が `user_submitted` と全く同じ流儀で render 境界において neutralize する。`user_submitted` と異なり sent-queue へのステージングは無い — intervention への回答は queued な inbox item だったことがないため、flow へ直接描画される |
| `reyn.event.inbox_cancel`            | 未 dispatch の queued user メッセージが id 指定でキャンセルされた(#3300 P3、`cancel_queued` client message 経由)— `msg_id` と `seq` を運ぶ。サーバー権威の sent-queue 除去シグナルであり(client-local な「キャンセル成功」応答ではない)、同じ `msg_id` について `turn_started` と排他である |
| `reyn.event.agent_delta`             | ストリーミングされた LLM content-delta チャンク 1 件(#3288 ③b)— `text`(raw な per-chunk delta)、`chain_id`、`round_index`(そのターンの何ラウンド目が生成したか、#3656)を運ぶ。tool を呼ぶターンは assistant メッセージを複数出すため `chain_id` だけでは区別できない。生産者はラウンドの内側で走るので、この番号は生産者が持っている事実であり、消費者がフレーム順から再構成するものではない。plain codec の `CUSTOM` マッピング(上記の note 参照)であり、実際の AG-UI ワイヤー(`encode_frame_wire_streaming`、#3288 ③d)ではこれは代わりに `TEXT_MESSAGE_CONTENT` に乗る——完全なストリーミングメッセージ contract(END のみの完了、再構成 authority、late-joiner closure)は上記 *Text lifecycle* を参照 |

### `reyn.intervention.<kind>`

上記の 2 つとは異なる形で運ばれる**open namespace**である: これは HITL **frontend-tool**
の `TOOL_CALL_START`(`CUSTOM` ではなく標準 event — *Intervention frontend-tool* 参照)の
`toolName` であり、そのため汎用 client は intervention を通常の tool call として描画・
回答できる。`<kind>` は intervention の種類(`ask_user`、`permission.*`、…)であり、呼び
出し元が与えるものであるため、これは閉じたメンバー集合としてではなく、**namespace** レベル
(固定された値 schema)でプロファイルされる。

- **`toolCallId`** — intervention id(client が `TOOL_CALL_RESULT` でそのまま echo し返す、
  回答の相関アンカー)。
- **`args`** — `{prompt, detail, choices, suggestions}`、汎用 client が質問を提示するため
  に描画するもの。

上記の `reyn.display.*` と `reyn.event.*` の namespace は、汎用 client が無視する
`CUSTOM`-event name である(スキップされ、致命的エラーにはならない); reyn client は
`_reyn` ブロックから正確な frame を再構成する。client がまだ知らない未知の `reyn.*` name
も同様にスキップされ、致命的エラーにはならない。

## Local ≡ remote

server は、ローカルの in-process transport が生成するのと**同一の**統一 frame stream
(display outbox + レンダラーに関連する audit-event の部分集合)をシリアライズする。AG-UI
transport が加えるのは wire framing のみであり、新しい render semantics は一切加えない —
そのため remote レンダラーの display バイト列と working-indicator の遷移は、ローカルの
ものと同一である。

**Local ≡ remote は input についても output と対称に成り立つ。** 解決された
intervention への回答(`InterventionHandler.deliver_answer_to` — 全ての回答経路が
共有する単一の funnel: TUI の自由記述回答、Textual TUI のグループ化された
intervention パネル(`reyn.interfaces.inline.textual_chat.intervention_panel`、
#3299 P1/P2、tab 化した #3308 P5 — **pending** な intervention ごとに 1 つの
tab を持ち、それぞれが closed-set の `RadioSet` か自由記述の `Input` であり、
会話領域と入力行の間に配置される。以前の in-flow chip surface を置き換えた。
ある tab に回答すると、その intervention の id を対象に配信され(id 指定の
delivery)、除去されずに ✓ / inert とマークされる——そのため同時に複数の
intervention が pending であっても、それぞれが順不同で独立に回答可能であり、
1 つが他方を押しのけることはない)、A2A peer、上記の AG-UI HITL round-trip)は
`intervention_answer_submitted` という audit-event を emit する(#3300 — 最後に
残っていた outbox `kind="user"` ブロードキャスト site を event 化したもので、下記の
`user_submitted` の前例に正確に倣う)。送信されたターン(`Session.submit_user_text`)は
兄弟にあたる `user_submitted` という audit-event を emit する(#3300 P1 C — 以前の
outbox-echo write を置き換えた。INPUT を display/OUTPUT channel に書き込むのは
category error だったため)。どちらも同一の統一 frame stream に
`EventFrame` として乗る(`_TURN_AND_ANSWER_EVENTS`、`transport/frames.py`)——
encode/decode は汎用的(`transport/agui/protocol.py`)なので、どちらの event type
についても wire 側の変更は不要だった。アタッチしているすべての surface の
event→display handler(`ConsoleChatRenderer.on_audit_event` /
`InlineChatRenderer.on_audit_event` / `TextualChatApp._pump_frames`)がその行を描画し、
その render 境界で neutralize する(`renderer.user_submitted_display_message` /
`renderer.intervention_answer_display_message` — Textual surface については
`TextualChatApp._handle_intervention_answer_event`)——**ただし自分の端末がすでに
それを表示していたクライアントを除く**、これは `user_submitted` にのみ当てはまる
(intervention への回答には重複排除すべきクライアントローカルな echo が存在しない —
panel/composer が回答そのものを描画することはないため、回答したクライアント自身を
含むすべての attached surface が、抑制ロジックなしにこの event から描画する)。plain な
PromptSession ループ(`--cui` / `chat.render_mode: plain` / non-TTY、
`stream_client.py`)では、対話的な TTY の `prompt_session.prompt_async` が Enter を
押した瞬間にその行を画面に残す——それ自体がすでに echo である。同じ送信から発生する
broadcast の `user_submitted` event で再度描画すると、LLM round-trip を伴うすべての
ターンで自分の行が二重に表示されてしまっていた(#3287。`/quit` は
`submit_user_text` に到達しないため二重化しない——バグ報告が指摘した非対称性その
もの)。修正は「デフォルトで抑制する」ではなく「所有権をはっきりさせる」こと、
そして transport の形ごとに**異なる 2 つの correlation 機構**を使う——どちらも
テキストではない:

- **ローカル(`InProcessTransport`)**: `route_input_line` は自身の
  `transport.submit_user_text` 呼び出しが**返す** `msg_id`(`user_submitted` の
  `msg_id` フィールドが運ぶのと同じ correlation id、#3300 P2a)を小さな set
  (`own_submissions`、クライアントの input/output ループのペアごとに保持し、
  他クライアントとは共有しない)に記録し、`run_output_loop` は `user_submitted`
  event の `msg_id` がこの set 内のエントリと一致したときだけ再描画をスキップ
  する。`ClientTransport.submit_user_text` はそのため割り当てられた `msg_id`
  を返す(以前は `None` だった)——`InProcessTransport` は
  `Session.submit_user_text` 自身の戻り値をそのまま返す(同一タスク内なので
  race フリー: audit-event の emit から呼び出し元へ id が届くまでの間に何も
  yield しない)。
- **リモート(`AgUiTransport`)**: 代わりに broadcast event の
  `meta.auth_connection_id` を、クライアント自身の `connection_id` と比較する
  (`remote_client.py` が起動時、submit よりも**前に** `uuid.uuid4()` で生成し、
  すべての POST に載せる id。AG-UI エンドポイントの `user_message` ハンドラは
  既にすべての submit にこの id を attribution として付与している——#3300 が
  マルチクライアント表示のために用意した既存の配線、`endpoint.py` →
  `session.py` の `meta` → broadcast event、という wire 形状は変わっていない)。
  この id は事前に分かっており、**他のどのチャンネルにも依存しない**——詳細は
  下記「race を狭めるのではなく閉じる」を参照。

いずれの機構でも、他のすべてのアタッチ済みクライアントのターン(および
non-interactive で何も echo していない場合の自分自身のターン)は今までどおり
描画される。2 クライアント以上がアタッチしていても、互いに相手のすべてのターンと
すべての回答は今までどおり見える——agent からの返信だけではない。変わるのは、
各クライアントが自分自身の行を二重に描画しなくなる点だけである。

相関は**テキストではなく identity**で行う——初期版はテキストで一致させており、
レビューで指摘された(#3309 の co-vet finding F1)。2 つのアタッチ済みクライアント
が同一の短い文字列(例えば両方とも "yes" と回答する)を送信すると、テキスト
一致ではクロスマッチしてしまう——他方のクライアントのターンを誤って抑制し、
その一方で自分自身のターンが後で二重に描画される、という形でバグが別の入口から
再発する。`msg_id`(#3300 P2a)と `auth_connection_id`(#3300、マルチクライアント
attribution)はどちらも**まさに identity フィールドとして**追加されたもの
(コンテンツから form-sniff したものでは決してない)なので、テキストが同一でも
2 つの異なる submission が衝突することはない。

**race を狭めるのではなく閉じる(#3309 の co-vet finding F2)**: 初期版は
remote 経路でも `msg_id` を使い、POST レスポンス body から読み取っていた——だが
この id は POST が返って初めて見えるようになる一方、server は同じ submission
の SSE broadcast を独立した events 接続経由で、その間にすでに push している
可能性がある——2 つのチャンネル間の network 到達順序に起因する race である。
レビュアーが指摘した通り、これは不要だった: `meta.auth_connection_id` は
クライアント自身の identity であり、submit が起きる**前から**分かっている——
これで一致させれば、解決すべき第 2 のチャンネルはそもそも存在せず、race を
「許容された残存事項として文書化する」のではなく構造的に閉じられる。`msg_id`
は remote 経路でも別の理由で load-bearing であり続ける——#3300 Y-client
(cancel-by-id)は transport に関わらずクライアントが自分の message id を
知る必要があるため、`AgUiTransport.submit_user_text` は引き続きそれを返す。
ただ remote の echo-suppression がそれで相関を取ることはもうない、という
だけである。

## AG-UI event coverage — 数字を正直に読む

**以下の数字にかかわらず、frame loss はゼロであり、reyn-client の fidelity は 100% で
ある。** すべての event は reyn-private な `_reyn` 再構成ブロックを運ぶ(上記の
*Standard envelope, reyn-private richness* 参照)。reyn client は常にこれから正確な元の
frame を復元する。このセクションの coverage の数字が記述しているのは別のことである:
**AG-UI の *標準* event 語彙のうちどれだけを** — reyn 固有の知識なしに描画できる、汎用の
非-reyn AG-UI client が見る信号を — reyn が現在ネイティブに発行しているか、対して汎用
client がスキップせざるを得ない `CUSTOM` event に折りたたんでいるか、である。ここでの
低い数字は、汎用-client の richness についての記述であり、data loss についての記述では
ない。

| Category   | Standard events | reyn-mapped | Disposition |
|------------|-----------------|-------------|--------------|
| State      | 3                | 3           | **complete** |
| Lifecycle  | 5                | 3           | **intentional-scope** — 2 つの Step event は、独立した標準 event としてではなく `STATE_*` read-model の `waiting_on` フィールドに fold される(上記 *STATE_\* — the status read-model* 参照) |
| Tool       | 5                | 3           | **complete for the HITL round-trip** — `TOOL_CALL_START` + `TOOL_CALL_END`(標準の `status` フィールド付き)+ `TOOL_CALL_RESULT`(intervention frontend-tool の回答 round-trip); `TOOL_CALL_ARGS`/`_CHUNK` のペアは **intentional-scope** である(reyn が発行する時点で tool call は既に完了しており、chunk 化すべき in-flight な args ストリームは存在しない) |
| Text       | 4                | 3           | **conforming triplet、plain と streamed 両方** — メッセージ 1 件全体が `TEXT_MESSAGE_START` → `TEXT_MESSAGE_CONTENT` → `TEXT_MESSAGE_END` に乗り、`messageId` で相関付けられる。ストリーミングしたメッセージ(#3288 ③a/③b/③d)は同じ triplet に、チャンクごとの本物の `TEXT_MESSAGE_CONTENT` を伴って乗る(上記 *Text lifecycle* 参照)——マップされていないのは凝縮された単一イベントの `TEXT_MESSAGE_CHUNK` variant のみである(**intentional-scope** — triplet の形式が既にストリーミングをカバーしており、reyn には代替の凝縮エンコーディングを使う理由がない) |
| Special    | 2                | 1           | **intentional-scope** — reyn-private なペイロードは常に構造化されている(`CUSTOM`)。標準の `RAW` passthrough event に reyn の use case はない |
| Activity   | 2                | 0           | **intentional-scope** — reyn に直接の analog はない。同じ情報は既に frame stream + `STATE_*` が運んでいる |
| Reasoning  | 7                | 3           | **standard-mapped** — reasoning メッセージ 1 件全体が `REASONING_MESSAGE_START` → `REASONING_MESSAGE_CONTENT` → `REASONING_MESSAGE_END` に乗り、`messageId` で相関付けられる。外側の `REASONING_START`/`REASONING_END` コンテキスト wrapper とストリーミング用の `REASONING_MESSAGE_CHUNK`/`REASONING_ENCRYPTED_VALUE` variant は **intentional-scope** である(reyn は whole-message であり、暗号化 CoT はない) |

**合計**: reyn は active-roster の標準 event **28 件中 15 件**をネイティブに発行している
(`CUSTOM` catch-all 自体を 1 件と数えると 16/28)。この 28 件の roster は、Lifecycle
(5)+ Text(4)+ Tool(5)+ State(3)+ Activity(2)+ Reasoning(7)+ Special(2)であり、
canonical な AG-UI event reference(<https://docs.ag-ui.com/concepts/events>)から集計
している。この reference は、active roster 外の meta/deprecated/draft entry を含めると
最大で ~34 件の event 名を自称している — 正確な数字は spec version に依存するため、この
ページは(より大きい数字ではなく)28 件の active roster を追跡対象とする。

### なぜこのようにギャップが disposition されているか

- **Reasoning(standard-mapped)。** reyn は reasoning を first-class な概念として扱って
  おり、reasoning display frame は標準の reasoning メッセージ triplet
  (`REASONING_MESSAGE_START` → `REASONING_MESSAGE_CONTENT` → `REASONING_MESSAGE_END`)に
  マップされるようになった。そのため汎用 AG-UI client は `CUSTOM` ペイロードをスキップする
  代わりに直接描画する。2 つの境界が尊重される(*reasoning lifecycle* 参照): **reasoning-display
  トグル**は構成によって守られる — reasoning frame は display が on のときのみ存在するため、
  display が off ⇒ `REASONING_*` event はゼロであり、マッピングは新しいゲートを追加しない —
  さらに reasoning chain-of-thought は display signal のみに留まり、observability export には
  決してルーティングされない(OTLP content-off デフォルトは影響を受けない)。外側の
  `REASONING_START`/`REASONING_END` wrapper とストリーミング chunk/encrypted variant は
  intentional-scope である(reyn は whole-message)。
- **intentional-scope とマークされたものはすべて**、見落としではなく本物のアーキテクチャ
  上の違い(reyn の whole-message outbox、構造化のみの private ペイロード、in-flight な
  tool-args フェーズが無いこと、直接の "activity" 概念が無いこと)を反映している —
  これらのギャップを埋めることは、バグを直すことではなく、reyn の設計が意図的に持ってい
  ない streaming/chunking の機構を発明することを意味する。
