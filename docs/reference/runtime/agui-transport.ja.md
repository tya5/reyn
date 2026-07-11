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
  - `{"type": "user_message", "text": "..."}` — ターンを submit する。
  - `{"type": "TOOL_CALL_RESULT", "toolCallId": "<intervention-id>", "text": "..."}` または
    `{..., "choiceId": "<id>"}` — pending 中の intervention に回答する(HITL の round-trip;
    下記「Human-in-the-loop answering」参照)。
  - `{"type": "cancel_inflight"}` — in-flight なターンを協調的にキャンセルする(Ctrl-C
    seam)。
  - `{"type": "heartbeat"}` — liveness の keepalive。

  server がモデル化していない入力 type は**グレースフルな no-op**(`200` の ack)であり、
  `500` にはならない — これが server 側の ignore-unknown である。
- `POST /agui/chat/{agent}/seize` — active-driver トークンを取得する(「Active driver and
  seize」参照)。

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
| `__attach_request__` | `CUSTOM`        | fail-safe プロファイルエントリ;upstream 消費(*control sentinels* 参照) |
| `__end__` / `__session_switch_request__` | *(フィルタ)* | 転送されない(*control sentinels* 参照) |

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
  - `__session_switch_request__` — 既に upstream(`registry.py:3061`)で swallow されており
    AG-UI tap に到達しない。フィルタは fail-safe。
- **upstream 消費 → fail-safe プロファイル**: `__attach_request__` は upstream
  (`registry.py:3052`)で swallow され tap に到達しない。そのプロファイルエントリは将来の
  tap-point 変更に対する fail-safe であり、live なワイヤー kind ではない(リモートの
  attach-label 同期はこのレガシーセンチネル経由ではなく別途設計される)。

#### Text lifecycle(適合する triplet)

AG-UI 仕様は、text lifecycle として**`TEXT_MESSAGE_START` → 1 つ以上の
`TEXT_MESSAGE_CONTENT` → `TEXT_MESSAGE_END`、すべて `messageId` で相関付けられる**ことを
必須としている。裸の `TEXT_MESSAGE_CONTENT` は不正である(厳格な汎用クライアントはそれを
破棄する)。したがって reyn のテキストメッセージ 1 件はこの triplet としてワイヤーに乗り、
メッセージごとに生成される id を伴い(reyn の outbox には安定した message id がない)、
CONTENT の `delta` がメッセージ全文を運ぶ(reyn は whole-message であり、トークン
ストリーミングは scope 外である)。

`_reyn` 再構成ブロックを運ぶのは **CONTENT** イベントのみである。START と END イベントは
汎用のスキャフォールドであり、reyn client はそれらを `None` にデコードして無視する。その
ため再構成の invariant は**1 フレーム ⇄ 1 つの `_reyn` 保持イベント**のままであり、reyn
client はメッセージごとにちょうど 1 つの display frame を再構築する。

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

| reyn chat-event               | AG-UI event      |
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
  `waiting_on`。
- `STATE_DELTA` — **変更時**に発行され、変更されたキーのみを運ぶ。アイドルなストリームは
  delta を発行しない。

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
| `reyn.display.user`               | user-authored な行 — 送信されたターン、または解決された intervention への回答 — が(agent の出力と同じ outbox fan-out 経由で)アタッチしている**すべての**クライアントへブロードキャストされる(生成元のクライアントだけではない)。`meta` はマルチクライアント描画向けに `auth_user_id` / `auth_connection_id` の attribution を任意で運ぶ(backlog の user ターンは代わりに標準の `messages` 配列に乗る) |
| `reyn.display.system`             | reyn chrome 行 — 永続化されるライフサイクル/ステータスマーカー(compaction / budget / cost-warn) |
| `reyn.display.__copy_last_reply__` | `/copy` センチネル — 転送される(クライアント側クリップボードコピー);*control sentinels* 参照 |
| `reyn.display.__rewind_list__`    | `/rewind` センチネル — 転送される(クライアント側 rewind ピッカー);*control sentinels* 参照 |
| `reyn.display.__attach_request__` | attach-request センチネル — fail-safe プロファイルエントリ(upstream 消費);*control sentinels* 参照 |
| `reyn.display.tool_call_started`  | tool-call 開始のトレース行                              |
| `reyn.display.tool_call_completed`| tool-call 完了のトレース行                              |
| `reyn.display.tool_call_failed`   | tool-call 失敗のトレース行                              |

### `reyn.event.<etype>`

標準的な AG-UI 対応物を持たない reyn の chat-event(working-indicator 軸)。`value` は
そのイベントのデータオブジェクトである。

| Custom `name`                        | Meaning                                          |
|--------------------------------------|--------------------------------------------------|
| `reyn.event.user_answered_intervention` | ユーザーが intervention に回答した              |

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
(display outbox + レンダラーに関連する chat-event の部分集合)をシリアライズする。AG-UI
transport が加えるのは wire framing のみであり、新しい render semantics は一切加えない —
そのため remote レンダラーの display バイト列と working-indicator の遷移は、ローカルの
ものと同一である。

**Local ≡ remote は input についても output と対称に成り立つ。** 送信されたターン
(`Session.submit_user_text`)と解決された intervention への回答
(`InterventionHandler.deliver_answer_to` — TUI の自由記述回答・TUI の選択肢リージョン・
A2A peer・上記の AG-UI HITL round-trip という全ての回答経路が共有する単一の funnel)は
それぞれ、agent の返信が乗るのと同じ `session.outbox` に `kind="user"` の frame を置く
ため、同一の `OutboxHub` ブロードキャストを経由して、アタッチしているすべての surface へ
fan-out される。送信したクライアント自身も(別のローカルエコーではなく)そのブロードキャ
スト frame から自分の行を描画する — 2 クライアント以上がアタッチしていれば、全員が
すべてのターンとすべての回答を見られる。agent からの返信だけではない。

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
| Text       | 4                | 3           | **conforming triplet** — メッセージ 1 件全体が `TEXT_MESSAGE_START` → `TEXT_MESSAGE_CONTENT` → `TEXT_MESSAGE_END` に乗り、`messageId` で相関付けられる。マップされていないのはストリーミング用の `TEXT_MESSAGE_CHUNK` のみである(**intentional-scope** — reyn の outbox はトークン差分ではなく whole message を配信する) |
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
