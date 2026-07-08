---
type: concept
topic: runtime
audience: [human, agent]
---

# エージェントライフサイクルフック

フックは、reyn セッションの 6 つの**ライフサイクルポイント**で、コンテキストの注入・自己継続トリガー・サンドボックス内副作用実行・pipeline 起動を行う、薄いオペレータースコープレイヤーです — あるいは、セッション自身のランループの外側で発火する**外部イベントポイント**でも(購読中の MCP resource の変更、監視ファイルの変更、cron ジョブの発火、または受信 webhook)。

フックは既存の 2 つの仕組みの上に構築されています: **統合インボックス**（ターンにメッセージを供給するチャネル）と **P6 ライフサイクル**（イベントストリーム）。新しい OS 機構は追加しません。フックを使うワークフローは OS の変更を必要としません（P7）。

## ライフサイクルポイント

フックはスコープと方向の組み合わせで 6 つのライフサイクルポイントに発火します:

| スコープ | `_start` | `_end` |
|---------|----------|--------|
| session | `session_start` | `session_end` |
| turn    | `turn_start` | `turn_end` |
| task    | `task_start` | `task_end` |

各ポイントは**awaited ディスパッチ**です。シェルの完了・プッシュのキューへの登録が終わってから、ライフサイクルポイントが次へ進みます。これにより、シェルフックはそのタイミングに同期的にアクセスできます（例: `session_start` シェルフックは最初のターンが始まる前に完了します）。

実装上のアンカー:

- `turn_end` は terminal `stop_reason` で発火
- `task_start` は Control IR の `_create` op で発火。`task_end` は `_update_status`（status → completed）と `_abort`（status → aborted）の両方で発火 — 開始したタスクは終了方法に関わらず必ず対応する `task_end` が発火する

## 外部イベントポイント

上記の 6 つのライフサイクルポイントとは異なり（セッション自身のターン/タスクランループから発火）、**外部イベントポイント**はそのループの外側で発火します: 現状、購読中の MCP resource の変更です。

### `mcp_resource_updated`

このセッションが `subscribe_mcp_resource` で購読した resource に対して、サーバが `resources/updated` 通知をプッシュしたときに発火します（[Resource subscriptions](../tools-integrations/mcp.ja.md#resource-subscriptions) 参照）。MCP receive-loop タスクから、境界付きキューを介してセッション自身のイベントループ上でドレインされます — エージェント自身のターン/タスクの仕組みからではないため、ターン/タスク境界だけでなくターンの間でも発火し得ます。

`template_push` / `pipeline_launch` のレンダリングで使えるテンプレート変数:

| 変数 | 意味 |
|-----|------|
| `server` | resource が属する MCP サーバー名。 |
| `uri` | 更新された resource の URI。 |
| `resync` | この発火が reconnect resync の場合 `true`、実際のサーバープッシュの場合 `false`。 |

**Reconnect 時の resync。** reyn は resource content のキャッシュを一切保持しないため、切断中の更新を見逃す可能性があります。トランスポート断からの reconnect が以前追跡していたすべての購読を再確立した後、以前購読していた各 URI に対してこのフックポイントが `resync: true` で一度ずつ発火します — 「切断中に変わったかもしれない、気にするなら読み直せ」という控えめなシグナルで、実際のプッシュと全く同じフックポイント・テンプレート変数の形を使います。セッションの最初の接続では決して発火しません（resync するものが無いため）。

### `file_changed`

operator が宣言した監視パス配下のファイルが作成・変更・削除されたときに発火します。`watchdog` extra(`pip install reyn[fs-watch]`)と、`reyn.yaml` の `fs_watch.paths` に少なくとも 1 つのパスが必要です — [reyn-yaml § `fs_watch` ブロック](../../reference/config/reyn-yaml.md#fs_watch-block) 参照。どちらか一方でも欠けていると、この機能はオフです(パスは設定されているが extra が無い場合は一度だけ警告がログされ、設定が全く無い場合はウォッチャーの無いビルドとバイト同一のまま静かに何もしません)。

テンプレート変数:

| 変数 | 意味 |
|-----|------|
| `path` | 変更されたファイルのパス。 |
| `event_type` | `created`、`modified`、`deleted` のいずれか。 |

監視パスは起動時に一度だけ、OUT-set(`reyn.yaml` / `reyn.local.yaml`)で宣言されます — エージェントが監視を登録したり広げたりできる op やツール verb はありません。ファイルシステム全体の変更フィードは、サンドボックスポリシーと同じクラスの懸念事項として扱われます。1 つの論理的な変更に対するイベントのバースト(エディタの一時ファイルの動き、create-then-modify)はパスごとにデバウンスされます — 1 つのバーストはフックを 1 回だけ発火させ、基盤となるファイルシステムイベントごとに発火することはありません。

### `cron_fired`

message ベースの `cron:` ジョブが自身のセッションに配送されるときに発火します。

テンプレート変数:

| 変数 | 意味 |
|-----|------|
| `job_name` | 発火したジョブの設定名。 |
| `to` | ターゲットのエージェント名。 |

### `webhook_received`

受信 webhook(Slack、LINE、汎用プラグイン)がセッションに解決されたときに発火します。

テンプレート変数:

| 変数 | 意味 |
|-----|------|
| `transport` | 論理トランスポート(`slack`、`line`、`webhook` など)。 |
| `sender` | 完全なルーティング sender 文字列(`"<transport>:<external_id>"`)。 |

テンプレートコンテキストは意図的にこのルーティングメタデータのみを運びます — **受信リクエストの生ボディは決して含みません**。それは operator が hook アクションに見せるつもりのなかったトークンや PII を運んでいる可能性があるためです。対照的に `cron_fired` の `job_name`/`to` は operator が記述した設定であり、エンドユーザー由来ではありません。

`cron_fired` と `webhook_received` はいずれも、**自身の ingress に対して非ブロッキング**です: cron ジョブ自身のインボックス配送、および webhook の HTTP レスポンスは、いずれも hook アクションを待ちません — ディスパッチは fire-and-forget のバックグラウンドタスクとしてスケジュールされるため、遅い hook(例えば数秒かかる `shell_exec`)がそれをトリガーした ingress をストールさせることはありません。

## Matcher: どのイベントがフックを発火させるかを絞り込む

フックは `matcher`（`dict[str, str]` のフィールド → パターン）を設定でき、フックのアクションが実行される**前**に、発火したイベントのテンプレート変数に対して評価されます:

```yaml
hooks:
  - on: mcp_resource_updated
    matcher: {server: "github", uri: "file:///repo/**"}
    template_push:
      message: "{{ uri }} changed on {{ server }}."
```

- matcher に列挙されたすべてのフィールドがマッチする必要があります: `uri` と `path` を除き**厳密な文字列一致**、`uri`/`path` はシェル風の glob（`fnmatch`）でマッチします — そのため `file:///repo/**` はそのプレフィックス以下のあらゆる URI に、`/repo/src/**` はその配下のあらゆる監視パスにマッチします。
- matcher に列挙されたフィールドが発火イベントに含まれない場合（例: ライフサイクルポイントのテンプレート変数には `server`/`uri` が無い）、**決してマッチしません** — matcher はイベントソースを絞り込むことしかできず、一度も発火していないシグナルを作り出すことはできません。
- **matcher が無い、または空 → フックは常に発火します** — デフォルトであり、`matcher` 以前のすべてのフックの挙動を変えません。

このルールはフックポイントではなくフィールド*名*にキーされています（`uri`/`path` は glob、それ以外は厳密一致）— そのため、将来の外部イベントソースが `uri` や `path` 形式のフィールドを発するようになれば、無料で glob マッチングが得られます。

## 4 つの設定スキーム

各エントリは相互排他な 4 つのスキームの**ちょうど 1 つ**を持ちます:

- **`template_push`** — 設定の Jinja2 テンプレートから組み立てるプッシュ指示。
- **`shell_exec`** — 純粋な副作用として実行するサンドボックスコマンド（出力は無視）。
- **`shell_push`** — **stdout が JSON プッシュ指示**であるサンドボックスコマンド。`template_push` と同じ経路でプッシュされます（違いは指示のソースのみ: キャプチャした stdout か Jinja2 レンダーか）。
- **`pipeline_launch`** — 発火イベントのテンプレート変数からレンダリングした input で、登録済みの [pipeline](pipelines.ja.md) を起動します。詳細は下記の [Pipeline launch](#pipeline-pipeline_launch) を参照。

## 4 つのケイパビリティ

これらのスキームは均一に 4 つの振る舞いケイパビリティを提供します:

### C — コンテキスト注入（`wake: false` のプッシュ）

`[hook:name]` 属性付きのシステムメッセージが統合インボックスにキューされます。**次のターン**で一緒に届きます — 追加ターンは発生しません。LLM がすぐに行動しなくてよい読み取り専用コンテキスト（メトリクス・タイムスタンプ・取得済みファクト）を付加するのに使います。`template_push` または `shell_push` の指示が `wake: false` のときに生成されます。

### E — 自己継続（`wake: true` のプッシュ）

C と同じですが、`wake: true` フラグがランループに新しいターンを即座に開くよう指示します。これがフックの差別化ケイパビリティです: `turn_end` フックは人間の入力なしにエージェントを再起動できます。[ループバルブ](#_6) で制限されます。`template_push` または `shell_push` が `wake: true` のときに生成されます。

### F — 外部副作用（`shell_exec`）

サンドボックス内でコマンドを実行します。reyn は JSON イベントをコマンドの stdin に書き込み、stdout と stderr は**無視**します。外部状態の更新（ログエントリの書き込み・メトリクスの発信・Webhook へのポスト）に使います。安全モデルは [サンドボックス](#_7) を参照してください。

### 計算されたプッシュ（`shell_push`）

**stdout** が単一の JSON オブジェクト `{"push_when": bool, "wake": bool, "message": str, "session"?: str}`（最初の 3 つは必須）であるサンドボックスコマンドです。stdout は `template_push` が生成するのと同じプッシュ指示にパースされ、同一の C/E 経路でディスパッチされます — つまりコマンドが*ランタイムに*プッシュするか（`push_when`）・どう（`wake`）・何を（`message`）を決定します。stdout は純粋な JSON である必要があります（ログは stderr へ）。いかなる失敗（非ゼロ終了・無効な JSON・必須/型不一致フィールド）も**プッシュをスキップ**します（フェイルセーフ）。ライフサイクルポイントは常に続行されます。`session` は**クロスセッションプッシュ**(下記参照)の宛先セッションを指定します — 省略時は現在のセッションがデフォルトです。

### Pipeline 起動(`pipeline_launch`)

発火したイベントのテンプレート変数から組み立てた input で、登録済みの [pipeline](pipelines.ja.md) を名前で起動します:

```yaml
hooks:
  - on: mcp_resource_updated
    matcher: {uri: "file:///repo/docs/**"}
    pipeline_launch:
      name: reindex_docs
      input_template: {uri: "{{ uri }}"}
```

- `name` — pipeline の登録名。ディスパッチ時に解決されます。登録されていない場合、フックは警告をログして起動をスキップします — ライフサイクル/外部イベントポイントは他のフック失敗と全く同様に、正常に完了します。
- `input_template` — 任意。`dict` の場合、その文字列リーフ(再帰的に)がそれぞれテンプレート変数に対して Jinja2 レンダリングされます。プレーンな文字列の場合、一度レンダリングされ、その出力が JSON オブジェクトとしてパースされます(`shell_push` の「stdout は JSON」契約を反映)。省略時は、pipeline は input なしで起動します。
- **非同期 / detached** で、どのフックポイント(ライフサイクルでも `mcp_resource_updated` でも)からも動作します: 起動は [`run_pipeline_async`](../../reference/runtime/pipeline-dsl.ja.md#_6) と同じ経路です — フックは fire-and-continue し、pipeline は自身の crash-recoverable な driver-session で実行され、結果は後でこのセッション自身のインボックスに `pipeline_result` メッセージとして届きます。

### クロスセッションプッシュ

`template_push` または `shell_push` 指示の `session` フィールドは、プッシュを現在のセッションではなく*別の*セッションのインボックスへルーティングします — ターゲットセッションは、自身のフックプッシュと全く同様にそれを処理します(`wake` は一緒に運ばれます: `true` はターゲットでターンをトリガーし、`false` はターゲットの次のターンにパッシブに乗ります)。現在のセッションを指定した場合、`session` を完全に省略した場合、クロスセッションルーティング能力のないコンテキストで実行している場合は、いずれもローカル(現在のセッション)プッシュにフォールバックします。

## wake フラグとランループ

`wake`（デフォルト `true`）が C と E を分けます。ランループは各ターン後にインボックスをドレインします:

1. キューされた全フックメッセージを収集します。
2. `wake: false` のメッセージは次のターンのコンテキストとして含められます（`wake: true` がなければ次の人間駆動ターンまで保留）。
3. `wake: true` が 1 つでも存在すれば、ループは**1 ターン**を発火します — 同じバッチの `wake: false` メッセージはそのターンのコンテキストとして一緒に届きます。

フックが設定されていないか、現在のライフサイクルポイントに一致しない場合、ループはフックなしのセッションとバイト同一です。ハッピーパスでオーバーヘッドはゼロです。

## 忠実性

プッシュは会話に追加される**新規の** `[hook:name]` 属性付きシステムメッセージです。既存の履歴を変更しません — オブジェクト同一性レベルで検証済みです（内容の等価比較ではなく）。

シェル出力は意図的に無視されます。reyn はトランスフォームフック（コンテキストやアーティファクトストリームを書き換えるフック）をサポートしません。実際のリダクション・トランケーション・コンテンツフェンスは OS レイヤーで実装されており、可視・イベント記録・監査可能です（[secret-handling](secret-handling.md) および [コンテンツレイヤー防御](../../reference/config/reyn-yaml.md#content-layer) を参照）。

## awaited ディスパッチアーキテクチャ

フックは `HookDispatcher` によってディスパッチされます。これは各ライフサイクルポイントでの第一級の同期 awaited 呼び出しです。EventLog サブスクライバーとは**異なります**:

| 仕組み | タイミング | 用途 |
|--------|----------|------|
| `HookDispatcher` | awaited 第一級 | フック — ライフサイクルポイントが次へ進む前に完了必須 |
| EventLog サブスクライバー | sync-inline、await なし | リアルタイムコンソールレンダー、アナリティクス |
| WAL | 追記専用の永続ログ | クラッシュリカバリ |
| P6 監査イベント | async-tolerant | 監査トレイル、リプレイ、eval |

サブスクライバーは sync-inline で `await` できません — emit 時点でのファイア＆フォーゲットです。プロセス終了を待つ必要があるシェルフックはサブスクライバーとして実装できません。`HookDispatcher` がこれを解決します。

各フックは独自の `try/except` ブロックでラップされます。フックの失敗はフック名に属して記録されますが、ライフサイクルポイントを中断したり LLM 出力に伝播したりしません。

## ループバルブ

E（自己継続）は暴走するフック駆動セッションを防ぐために制限されています:

- **カウンター**: `safety.loop.max_hook_driven_turns`（デフォルト `25`）は最後の人間ターン以降のフック駆動ターン数をカウントします。
- **リセット**: カウンターは人間ターンのたびにゼロにリセットされます。
- **上限到達時**: 設定された `safety.on_limit` アクションが発火します — `warn` → `ask_user` → `abort`。いずれもセッションを生かし続けます（サイレントキルなし）。
- **制限なし**: `max_hook_driven_turns: 0` に設定すると上限を無効化できます。

バルブは障壁ではなく安全網です。適切に設計された自己継続フックはキャップに達する前に完了します。バルブはバグや予期しないワークフローの動作によって開きっぱなしになるループを捕捉します。

## サンドボックス

シェルフックは、Control IR の `shell_exec` op と同じバックエンド非依存のサンドボックス抽象化の中で実行されます: Seatbelt（macOS）、Landlock/seccomp（Linux）、Noop（非対応プラットフォーム）、またはコンテナバックエンド。安全なデフォルトが適用されます:

- `network: false` — アウトバウンドネットワークをブロック
- サブプロセス生成なし
- コンセント失敗クローズ: サンドボックスバックエンドが確認できない場合、サンドボックスなしで実行するのではなくシェルフックを拒否します

### コンセントと許可リスト

シェルフックコマンドを実行する前にオペレーターのコンセントが必要です。コンセントフローはライブの介入リスナーが接続されているかによって変わります:

- **対話的チャットセッション**(インライン CUI) — コンセントは統合介入バスを通じてルーティングされ、入力欄上部のリージョンに選択式介入として表示されます: "Shell hook `<name>` wants to run a command"（フックに設定された `name:` フィールド、または未設定の場合は汎用メッセージ）。3 つの選択肢:
  - **[A]lways** — 許可し許可リストに永続化します（`~/.reyn/shell-hooks-allowlist.json`、`REYN_SHELL_HOOKS_ALLOWLIST` 環境変数で上書き可）。同じコマンドの将来の実行は自動承認されます。
  - **[y]es** — 今回の実行のみ許可します。
  - **[n]o** — スキップ（失敗クローズ）。
- **非対話的**(`reyn run`、`mcp-serve`、ヘッドレス) — バス使用前の動作にフォールバック: TTY stdin が利用可能なら stdin プロンプト、TTY でない場合は拒否。
- **許可リストヒット** — 許可リストに既存のコマンドはすべてのサーフェスでプロンプトなしにサイレント自動承認で実行されます。

コンセントは全体を通じて失敗クローズです: サンドボックスバックエンドが確認できない場合、サンドボックスなしで実行するのではなくフックを拒否します。

フルバックエンドモデルは [sandbox](sandbox.md)、より広いコンセントアーキテクチャは [permission model](permission-model.md) を参照してください。

### P6 イベント: `hook_shell_executed`

すべてのシェルフック実行 — サイレント自動承認の実行も含む — は `hook_shell_executed` P6 イベント("tool" グループ)を発行し、次を記録します:

```
shell_exec: <コマンド> [rc=N]
```

（プッシュモードのフックは `shell_push:` プレフィックスになります。）コマンドが exit 0 の場合、リターンコードのサフィックスは省略されます。これにより、コンセントパスに関わらずシェルフックアクティビティの完全な監査トレイルをオペレーターに提供します。

## 設定

フックは `reyn.yaml` の `hooks:` キーの下で宣言します。フルスキーマは [reyn-yaml リファレンス § hooks ブロック](../../reference/config/reyn-yaml.md#hooks-block) を参照してください。

簡単な例 — `turn_end` 自己継続 `template_push`、`session_start` `shell_exec`、stdout がプッシュを決める `turn_end` `shell_push`、matcher で絞り込んだ `mcp_resource_updated` の `pipeline_launch`:

```yaml
hooks:
  - on: turn_end
    template_push:
      message: "Run complete. Check for pending tasks."
      wake: true

  - on: session_start
    shell_exec: "echo session-started >> /tmp/reyn-hooks.log"

  - name: dynamic
    on: turn_end
    shell_push: "scripts/decide-next.sh"   # {"push_when":true,"wake":true,"message":"..."} を出力

  - on: mcp_resource_updated
    matcher: {server: "github", uri: "file:///repo/docs/**"}
    pipeline_launch:
      name: reindex_docs
      input_template: {uri: "{{ uri }}"}
```

最初のフックの `wake: true` は各 `turn_end` の後に新しいターンをトリガーし、メッセージをシステムコンテキストとして注入します。`session_start` の `shell_exec` はログ行を追記します（出力は破棄）。`shell_push` はコマンドを実行し stdout をパースし、指示がそう言うときだけプッシュします。最後のフックは `github` サーバーの `docs/` 配下の resource に対してのみ発火し — 発火した際は、変更された URI を input として `reindex_docs` pipeline を非同期に起動します。

## 未実装(Deferred)

以下のケイパビリティは設計済みですが未実装です:

- **エージェントレベル・フェーズレベルフック** — ターン内の細粒度ポイント(稀なユースケース。session/turn/task が一般的なケースをカバー)。

## 参照

- [ワークスペース](workspace.md) — フックプッシュメッセージが着地する単一情報源
- [イベント](events.md) — フックディスパッチを記録する P6 監査トレイル
- [パーミッションモデル](permission-model.md) — シェルフックのコンセントフロー
- [サンドボックス](sandbox.md) — シェルフックの実行環境
- [reyn-yaml § hooks](../../reference/config/reyn-yaml.md#hooks-block) — フル設定リファレンス
- [MCP § Resource subscriptions](../tools-integrations/mcp.ja.md#resource-subscriptions) — `mcp_resource_updated` 外部イベントポイントの発生源
- [Pipeline](pipelines.ja.md) — `pipeline_launch` フックが起動するもの
