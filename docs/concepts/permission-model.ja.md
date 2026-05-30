---
type: concept
topic: architecture
audience: [human, agent]
---

# パーミッションモデル

reyn のパーミッションシステムは 4 種類のケイパビリティをゲートします：ファイルパス、シェル、MCP ツール呼び出し、Python プリプロセッサーステップです。デフォルトは保守的です。それ以外はすべて Skill が宣言し、ユーザーが承認する（または `reyn.yaml` で事前承認する）必要があります。

## 3 つのレイヤー（順番通り）

```
┌──────────────────────────────┐  常に許可。宣言不要
│  デフォルト（読み取り専用プロジェクト）│
└──────────────────────────────┘
             ↓ Skill がさらに必要とする場合
┌──────────────────────────────┐  skill.md frontmatter で宣言。ユーザーが承認
│  Skill 宣言                  │  承認は .reyn/approvals.yaml に永続化
└──────────────────────────────┘
             ↓ プロジェクトを広く信頼する場合
┌──────────────────────────────┐  reyn.yaml: permissions.<key>: allow
│  プロジェクト全体の事前承認    │  そのケイパビリティのプロンプトをバイパス
└──────────────────────────────┘
```

### レイヤー 1：デフォルト

プロジェクトルート配下のどこでも読み取り/glob/grep。書き込み/編集/削除は `.reyn/` または `reyn/` 配下のみ。シェル、MCP、Python は不可。

### レイヤー 2：Skill 宣言

デフォルト外のものが必要な Skill は `skill.md` frontmatter でパーミッションを宣言します。Skill の起動時、ランタイムは単一の承認プロンプトを表示します：

```
[approval] my_skill/file.write needs:
  /tmp/output (just_path)

  [y] この実行のみ許可
  [j] この正確なパス + Skill について永続化
  [r] 親ディレクトリ（再帰的）+ Skill について永続化
  [N] 拒否
```

永続的な選択は `.reyn/approvals.yaml` に `<skill>/<op>/<path>` のキーで保存されます。キーは Skill スコープです。ある Skill の承認が別の Skill に漏れることはありません。

### レイヤー 3：プロジェクト全体の事前承認

`reyn.yaml` でプロジェクト全体のケイパビリティを事前付与できます：

```yaml
permissions:
  shell: allow
  file.write: allow
  python:
    safe: allow
    unsafe: allow
```

控えめに使いましょう。`allow` はプロンプトを完全に削除します。

## 非インタラクティブ実行

`reyn eval` はプロンプトなしで実行されます。承認は事前に整っている必要があります。`reyn.yaml` で事前承認されているか、以前のインタラクティブ実行から `.reyn/approvals.yaml` に永続化されているかです。

これは同じ信頼モデルです。eval が何が安全かを決めるのではなく、あなたが事前に決めます。

## なぜ Skill スコープのキーなのか

承認はグローバルではなく Skill でキー付けされます。Skill A が「`/tmp/foo` に書き込んでよいか？」と尋ね、それを承認しても、Skill B に同じアクセスを付与することにはなりません。

理由はコンポジションの安全性です。Skill A は信頼されているかもしれません。Skill A が（`run_skill` を通じて）サブスキル B を呼び出しても、B のパーミッションが推移的に付与されるわけではありません。B は自分自身のために求める必要があります。

## `mcp_install` パーミッション {#mcp_install-パーミッション}

> [Collapse arc](#collapse-arc571) 中の compat shim 形式。 canonical な decomposition は `file.write: [.reyn/mcp.yaml]` + `http.get: [{host: registry.modelcontextprotocol.io}]` + `secret.write: [<env_key>]`、 下の bool 形式は Phase 4 まで維持される。

`mcp_install` は **設定への新しい MCP サーバーの追加** をゲートします。これはランタイムのツール呼び出しをゲートする `permissions.mcp` とは別物です。

```yaml
permissions:
  mcp_install: ask      # deny | ask | allow （デフォルト: ask）
```

| 値 | 動作 |
|-------|-----------|
| `ask`（デフォルト） | サーバー ID ごとの初回インストール時にインタラクティブプロンプト。承認は `mcp_install:<server_id>` キーで `.reyn/approvals.yaml` に永続化されます。 |
| `allow` | プロンプトなしでインストール。 |
| `deny` | すべてのインストール試行を即座に拒否。 |

### スコープ層

`mcp_install` は標準の 3 層マージに参加します：

```yaml
# ~/.reyn/config.yaml（ユーザースコープ）
permissions:
  mcp_install: allow     # 個人の開発機 — フリクションなし

# <project>/reyn.yaml（プロジェクトスコープ — git にコミット）
permissions:
  mcp_install: deny      # チーム共有プロジェクト — サーバーリストは一元管理

# <project>/reyn.local.yaml（ローカルスコープ — gitignored）
permissions:
  mcp_install: ask       # このプロジェクトの個人オーバーライド
```

### エンタープライズユースケース: 「承認済みサーバーのみ」ポリシー

`mcp_install: allow` とプライベートレジストリを組み合わせて、インストールを許可しながら見えるサーバーを制限します：

```yaml
# enterprise reyn.yaml（プロジェクトスコープ）
mcp:
  registries:
    - https://mcp-registry.internal.acme.com/    # プライベートレジストリ（承認済みサーバーのみ）
    - https://registry.modelcontextprotocol.io/   # パブリックフォールバック（優先度低い）
permissions:
  mcp_install: allow
```

この設定でチームメンバーは `reyn mcp install <id>` を自由に実行できますが、プライベートレジストリに登録されたサーバーのみが検索可能です。パブリックレジストリはフォールバックですが、そこからインストールされるサーバーも同じ監査証跡（`mcp_server_installed` イベント）を通ります。レジストリの順序でパブリックパスを事実上制限することで、`deny` パーミッションレベルを必要とせず多層防御を実現します。

### 監査証跡

インストールが成功するたびに `server_id` と `scope` を持つ `mcp_server_installed` イベントが発行されます。フィルタリング：

```bash
grep '"mcp_server_installed"' .reyn/events.jsonl
```

## パーミッション Tier モデル (FP-0022)

Reyn のパーミッションは 2 つの軸で機能します：

**軸 1 — 使用宣言** (skill.md frontmatter の `permissions:` ブロック):
Skill の作者が使用する op を宣言します。宣言されていない op は即座に `PermissionError`
を発生させます（Android のマニフェストに登録されていない API を呼び出した場合の
`SecurityException` に相当）。

**軸 2 — 認可** (オペレーター / ユーザーによるアクセス付与):
`PermissionResolver._approve()` の 4 層解決：

| レイヤー | 提供元 | 永続性 |
|---|---|---|
| 1 | `reyn.yaml` `permissions.<key>` | 静的設定 |
| 2 | `.reyn/approvals.yaml` | セッション横断 |
| 3 | インメモリセッション決定 | セッションのみ |
| 4 | インタラクティブプロンプト | → レイヤー 2 または 3 |

### Op Tier 分類

| Tier | 代表的な op | 宣言 | デフォルト | 設定制限 |
|---|---|---|---|---|
| 0 | `run_skill`, `ask_user` | 不要 | 無条件パス | 不可 |
| 1 | `web_search`, `web_fetch` | 不要 | allow | `deny` でブロック |
| 2 | `mcp` | 必須 | ask (4 層) | `allow` で事前承認 |
| 3 | `shell`, `file` (ゾーン外) | 必須 | ask (4 層) | `allow` で事前承認 |

Tier 0 は「デフォルト allow」ではなく「無条件パス」です。これらの op をブロックする
設定キーは存在しません（存在するとスキルの実行セマンティクスが破壊されます）。

### web_fetch の動作変更 (FP-0022)

FP-0022 以前: `reyn.yaml` に `web.fetch: allow` が必要でした。未設定の場合、
ツールはルーターのカタログから非表示になりました（サイレントに利用不可）。
ユーザーが何かを調べるよう依頼しても、プロンプトなしで拒否される混乱した UX でした。

FP-0022 以降: 4 層承認によるデフォルト allow。ツールは常にルーターカタログに含まれます。
初回使用時にインタラクティブプロンプトが発火します（YES/NO/ALWAYS/NEVER）。
`web.fetch: allow` は事前承認します（既存の動作を保持）。`web.fetch: deny` は即座にブロックします。

### web_search の設定制限 (FP-0022)

`web_search` は `reyn.yaml` の `web.search: deny` を尊重するようになりました
（即座に `PermissionError`）。デフォルトは allow です。web 検索は読み取り専用で
副作用がないため、オペレーターの `deny` のみが合理的な制限パスです。インタラクティブプロンプトは不要です。

### web_fetch と MCP レジストリの SSL 設定 (FP-0022 follow-up)

`reyn.yaml` で `web_fetch` と MCP レジストリリクエストの SSL 設定を宣言的に行えます。
env var を使わず、設定ファイルレベルで企業 MITM プロキシ / カスタム PKI の
ユースケースを解決します。

```yaml
web:
  fetch:
    verify_ssl: false          # bool — SSL 検証を完全に無効化
    ca_bundle: /path/to/ca.pem # str  — カスタム CA バンドルファイルパス
```

両フィールドはオプションです。優先順位（高い方から）：

| 優先度 | 設定元 | 効果 |
|---|---|---|
| 1 | `web.fetch.ca_bundle` 設定あり | httpx に `verify=<path>` を渡す（カスタム CA） |
| 2 | `web.fetch.verify_ssl: false` | SSL 検証を無効化（`verify=False`） |
| 3 | `web.fetch.verify_ssl: true` | SSL 検証を強制（`verify=True`） |
| 4 | 両方未設定（デフォルト） | `SSL_VERIFY` env → `litellm.ssl_verify` → `SSL_CERT_FILE` → `True` |

両方設定された場合、`ca_bundle` が `verify_ssl` より優先されます。
どちらも設定されていない場合は既存の `SSL_VERIFY` / `SSL_CERT_FILE` env var の
動作がそのまま維持されます（env var を使っている既存環境への影響なし）。

**主なユースケース：**

- **企業 MITM プロキシ（内部 CA あり）**: `ca_bundle: /etc/ssl/certs/corp-ca.pem` を設定
- **自己署名証明書の開発環境**: `verify_ssl: false` を設定
- **env var に関係なく SSL 検証を強制**: `verify_ssl: true` を設定

## Permission は OS の I/O primitive

permission system は OS runtime の一部であり、 OS の上に乗る別レイヤーではない。 reyn が行う全 side-effect — skill code 由来も、 op handler 由来も、 その他 OS 内部経路由来も — すべて同じ permission resolver を、 呼び出し起点 skill の `PermissionDecl` に対して通る。 inside / outside の区別はない: OS は permission system を、 全 I/O に対する自身の core abstraction として用いる。

具体例: `op_runtime/mcp_install.py` が `.reyn/mcp.yaml` を write するとき、 これは `reyn.safe.file.write` を経由する — skill-level の safe-mode python step が使うのと同じ gate。 スコープ上の PermissionDecl は呼び出し起点 skill のもの、 OS は呼び出し元に関係なく一様に honor する。 旧来の 「OS は caller を gate するが自分自身は gate しない」 framing は、 この単一機構で解消される — 循環の懸念もない。

## 宣言軸の taxonomy

各 side-effect 種別ごとに対応する宣言軸がある。 軸語彙は小さく保たれ、 **bool 軸は真に capability-shaped な操作 — 単一の file / network / secret I/O scope に reducible でないもの — に予約**されている。

### 軸

| 軸 | 型 | 粒度 | gate site | 補足 |
|---|---|---|---|---|
| `file.read` | `list[{path, scope}]` | per-path | `require_file_read()` | scope ∈ {`just_path`, `recursive`} |
| `file.write` | `list[{path, scope}]` | per-path | `require_file_write()` | write / edit / delete を包含 |
| `http.get` | `list[{host}]` | per-host | `require_http_get()` | specific host = startup prompt → silent runtime / `"*"` wildcard = runtime per-host prompt。 `reyn.safe.http.*`（skill-internal、 specific のみ）と `web_fetch`（LLM-driven、 wildcard 受容）の両 surface を統一 |
| `secret.write` | `list[<key>]` | per-key | `require_secret_write()` | `~/.reyn/secrets.env` write の per-key、 `"*"` wildcard で runtime-determined keys 対応（= value prompt が actual gate）|
| `mcp` | `list[str]` | per-server | MCP 呼び出し時 implicit | サーバー名の allowlist |
| `python` | `list[{module, function, mode, timeout}]` | per-step | `require_python_step()` | mode ∈ {`safe`, `unsafe`} |
| `tool` | `list[str]` | per-tool | `require_tool()` | 名前指定 tool allowlist |
| `shell` | `bool` | abstract | `require_shell()` | binary（= shell 全般へのアクセス）|
| `allowed_mcp` | `list[str] \| None` | ACL filter | MCP 呼び出し時 implicit | per-agent restriction、 `mcp` 軸と cross-cut |

### `shell` だけが bool である理由

`shell` は任意 command の process exec。 side-effect 集合が unbounded（= shell command は任意 file の read / 任意 file の write / 任意 host への network ができる）で、 作者は具体的な invocation がどの side-effect を引き起こすかを enumeration できない。 単一 I/O scope に reduce する余地がない — process exec **そのものが** irreducible primitive。

他の旧 bool 軸（`mcp_install` / `mcp_drop_server` / `cron_register` / `index_drop`）はいずれも、 実際には小さな file / network / secret 操作の集合に reducible であることが判明したため、 1 つ以上の list 軸へと表現し直された:

| 旧 bool 軸 | 等価な list 軸 decomposition |
|---|---|
| `mcp_install: true` | `file.write: [.reyn/mcp.yaml]` + `http.get: [{host: registry.modelcontextprotocol.io}]` + `secret.write: [<env_key>]` |
| `mcp_drop_server: true` | `file.write: [.reyn/mcp.yaml]` |
| `cron_register: true` | `file.write: [.reyn/cron.yaml]` |
| `index_drop: true` | `file.write: [.reyn/index/sources.yaml]` + `.reyn/index/<source>/index.db` の delete |

criterion: **capability が有限の I/O scope（file path / host / secret key）に reducible なら list 軸、 そうでなければ bool**。 現状、 唯一の irreducible primitive は shell のみ。

### collapse で失ったもの・失わなかったもの

bool 軸は per-instance approval surface を持っていた（= `mcp_install:<server_id>` のように server ごとに key 化）。 collapse 後:

- **MCP の per-server 粒度は保持される**: call 時点で既存 `permissions.mcp: [<server>]` 軸が依然として per-server gate を効かせる。 server install（= `.reyn/mcp.yaml` への write）は 1 段階の grant になるが、 specific server を使う段階で再度 per-server check が走るため、 server package の download + execute は call-time gate の範疇に収まる。
- **cron の per-job 粒度は減る**: 「`.reyn/cron.yaml` に書ける」 の 1 段階に集約される。 ただし cron 発火で起動される skill は実行時に自身の permission gate を再度通るため、 下流の保護は bypass されない。
- **index の per-source 粒度は減る**: post-write gate に相当するものはない。 drop は destructive op であり、 per-source 区別は security ではなく operator UX の話だったので、 粒度減は accept する。

### `allowed_mcp` は ACL filter であって capability ではない

`allowed_mcp` は capability を grant しない — すでに grant 済の `mcp` server list の **subset を agent ごとに restrict** する。 ACL filter は capability 軸と cross-cut する別系統。

## Trust boundary レイヤー

side-effect を実行する surface を強制力の強い順に:

```
┌──────────────────────────────────────────────────────────────┐  ← 最強
│  sandboxed_exec op (FP-0017)                                 │
│    OS-kernel 強制（Seatbelt / Landlock / Seccomp）            │
│    per-call で argv-scoped / network-scoped / fs-scoped       │
├──────────────────────────────────────────────────────────────┤
│  safe-mode python step (FP-0042)                             │
│    AST validation（= compile-time に `import os` 等を reject） │
│    + reyn.safe.* honor-system による per-call path check       │
│    kernel sandbox なし、 subprocess は user UID で動作         │
├──────────────────────────────────────────────────────────────┤
│  unsafe-mode python step                                     │
│    `--allow-unsafe-python` opt-in 後は gate なし              │
│    宣言による trust（= 作者が step の安全性を保証）            │
├──────────────────────────────────────────────────────────────┤
│  reyn パッケージ内部（op handler / registry client）          │
│    skill code と同じ `reyn.safe.*` primitive を、 呼び出し     │
│    起点 skill の PermissionDecl に対して使用                  │
└──────────────────────────────────────────────────────────────┘
```

- **最上層 (sandboxed_exec)** は OS-kernel 強制を持つ唯一のレイヤー。 argv / network / fs scope を per-call で declarative 宣言、 platform sandbox が強制。
- **内部 OS code** は skill code と同じ `reyn.safe.*` primitive を、 呼び出し起点 skill の PermissionDecl に対して使う。 inside / outside の区別はない — OS は自身の permission 機構を一様に使う。
- **safe-mode python** は honor system: AST validation が `import os` を弾き、 `reyn.safe.*` が宣言済 path / host / key を check する。 motivated な user が `mode: unsafe` で bypass する余地はあるが、 通常の `mode: safe` author が accidentally bypass することはない。
- **unsafe-mode python** は宣言 trust: operator が runtime に `--allow-unsafe-python` を承認、 step が host へのフルアクセスを持つことを accept する。

## 業界比較

| Platform | 宣言 shape | runtime ask | 粒度 | 強制レイヤー |
|---|---|---|---|---|
| iOS (TCC + Entitlements) | `Info.plist` capability + purpose string | First-use prompt | Capability axis | OS kernel + signed entitlements |
| Android (≥ M) | `AndroidManifest.xml` `uses-permission` | dangerous tier に first-use prompt | Permission class + scoped storage | OS kernel + per-app UID |
| Web Permissions API | feature ごとに query | per-permission prompt | Origin-scoped（= per-domain capability）| Browser sandbox |
| Anthropic Claude Code | Tool list（Bash / Edit / Read / Write）| デフォルトでは無、 sandbox-mode で opt-in | Tool 名（path scope なし）| Seatbelt（sandbox-mode）または trust |
| MCP servers | server side で tool list 公開 | server がオーナーシップ | per-tool、 server 定義 | プロセス境界 |
| **Reyn** | `permissions:` ブロック（list 軸主体、 bool は `shell` のみ）| startup_guard + first-use interactive | per-path / per-host / per-server（resource scope）| safe-mode は AST + `reyn.safe.*` honor system / `sandboxed_exec` のみ kernel |

Reyn は iOS / Android の 「capability + first-use prompt」 主流から 2 軸で乖離する:

1. **粒度は業界標準より細かい** — list 軸の path / host / server scope は iOS / Android の capability axis より Web の origin-scope に近い。 正当化: Reyn skill は workflow code（= 作者が inventory を知る）、 iOS / Android app は general-purpose。
2. **safe-mode python の強制は honor system** — iOS / Android は kernel boundary、 Reyn は AST validation + path / host / key の check を `reyn.safe.*` primitive 経由で行う。 Trade-off: 実装簡素（= per-step seatbelt セットアップ不要） vs 強制の弱さ。

## Collapse arc（#571）

上の軸 taxonomy は target state。 パーミッション監査で、 旧設計が `mcp_install` / `mcp_drop_server` / `cron_register` / `index_drop` の 4 つの bool 軸を持っており、 これらが `file.write` 軸と重複していることが識別された — side-effect はいずれも canonical な `.reyn/*.yaml` への write に reducible で、 そして `reyn.safe.file.write` 経由で到達可能であったため、 bool 軸は新たな capability を gate するのではなく既存 capability を二重 gate していた。 Collapse arc はこれを段階的に除去する:

| Phase | scope | 状態 |
|---|---|---|
| 1 | 本 doc — 「permission is an OS I/O primitive」 と collapse map を articulate | 本 PR |
| 2 | `op_runtime` handler（= `mcp_install` / `mcp_drop_server` / `cron_register` / `index_drop`）を `reyn.safe.file.write` 経由化、 loader compat shim で bool 形式と list 形式を両方受け付け | 後続 PR |
| 3 | `http.get: [{host}]` 軸（= `reyn.safe.http.*` を per-host gate）と `secret.write: [<key>]` 軸（= `~/.reyn/secrets.env` write を per-key gate）を新設 | 後続 |
| 4 | stdlib skill 全体を明示 list 形式に移行 | 後続 |
| 5 | bool 軸（`mcp_install` 等）と `require_mcp_install` / `require_cron_register` / `require_index_drop` / `require_mcp_drop_server` を OS surface から撤去 | 後続 |

Phase 1–4 の間、 bool 形式（= `mcp_install: true`）は compat shim として受け付けられ、 暗黙的に等価な list 軸 decomposition に展開される。 Phase 5 で bool 形式は撤去される。

### Phase 7 — prompt-timing model 統一 + `safe.http` / `web_fetch` collapse

Phase 7 は `http.get` 軸を `file.write` と同じ prompt model に揃えて alignment を仕上げる:

- **Specific declared host**（`http.get: [{host: "api.github.com"}]`）— `startup_guard` が `<skill, host>` ごとに 1 回 operator に prompt し、 結果を approvals.yaml に `<skill>/http.get/<host>` で persist。 runtime は silent。 default zone 外 path に対する `file.write` と同 pattern。
- **Wildcard**（`http.get: [{host: "*"}]` または `["*"]`）— host が write-time に不明（= LLM が runtime に決める、 例: `web_search` 結果 URL を `web_fetch` で follow）なので、 prompt は `require_http_get` 内の実 host gate で fire。 persistence key も同形 `<skill>/http.get/<host>`、 ALWAYS / NEVER は per-host で効く。
- **宣言なし** — legacy `web.fetch` compat path + `DeprecationWarning`、 segmented migration window 期間中。 Tier-1 default-allow に依存していた既存 skill はそのまま動く。

`web_fetch` op handler は legacy `require_web_fetch` でなく `require_http_get` 経由化、 chat router の PermissionDecl は `http.get: [{host: "*"}]` 宣言で LLM-driven fetch を wildcard branch に流す。 `reyn.safe.http` subprocess path は preprocessor で wildcard entry を strip — sync subprocess は prompt 不可なので wildcard host fetch は `web_fetch` op route 必須。

これで 2 surface（`safe.http` skill-internal + `web_fetch` LLM-driven）が 1 軸 + 1 prompt model に統一される。 browser extension `host_permissions`（= 宣言 + install-time prompt）+ Web Permissions API（= runtime per-feature prompt）の合成 pattern に対応 — [業界比較](#業界比較) 参照。

| 観点 | Pre-Phase-7 | Post-Phase-7 |
|---|---|---|
| `safe.http` skill-internal | per-host decl、 runtime silent、 prompt なし | specific decl 不変、 wildcard 拒否（= subprocess prompt 不可）|
| `web_fetch` LLM-driven | Tier-1 default-allow、 4-layer per-URL prompt | `http.get` 軸経由、 chat router decl が wildcard を carry して挙動保持 |
| Operator prompt 粒度 | per-URL（`web.fetch` key）| per-host（`<skill>/http.get/<host>` key）— ALWAYS で 1 host 全 URL カバー |
| Skill author の LLM fetch scope 制御 | なし | specific host 宣言で LLM の fetch 範囲を制限可能（= 宣言 host のみ allow、 wildcard 不在 = fallback なし）|
| Legacy `web.fetch: allow` / `deny` config | 直接 gate | migration window 中、 `require_http_get` 内で backward-compat alias として honored |

## `python` パーミッションと `mode: safe` allowlist

`python` パーミッションには 2 段階あります:

| 段階 | 設定キー | 許可する内容 |
|-------|-----------|----------------|
| `safe` | `python.safe: allow` | `PURE_STDLIB_ALLOWLIST` に含まれるモジュールのみ import 可能なステップ — clock、entropy、純粋計算、および `__future__`（コンパイラディレクティブ）。ファイルシステム・ネットワーク・プロセスへのアクセス不可。 |
| `unsafe` | `python.unsafe: allow` | ファイルシステム・ネットワークを含む任意モジュールの import が可能なステップ。 |

`PURE_STDLIB_ALLOWLIST` は `src/reyn/kernel/_python_allowlist.py` で定義されています。`__future__` はコンパイラディレクティブとして一覧に含まれており、ランタイムのケイパビリティを持ちません。

**非インタラクティブ自動許可**: stdlib スキルが `reyn run`（非インタラクティブコンテキスト）経由で呼び出される場合、`mode: safe` と `mode: unsafe` の両方の python ステップはプロンプトなしで自動許可されます。これは eval / CI 実行で他の op に既に適用されている非インタラクティブ動作と同等です。

**`mode: safe` の形式的契約**（= "ambient sources only"）は [Python safe モード](python-safe-mode.ja.md) で文書化されています。allowlist の根拠・コンテキスト別の safe/unsafe 自動許可ルール・unsafe ステップを safe に変換するリファクタリングパターンを網羅しています。

## スキルごとのクレデンシャルスコーピング (FP-0016 D)

### 脅威モデル：Confused Deputy

親スキルが `run_skill` でサブスキルを呼び出す際、スコーピングが適用されていないと
サブスキルは親の全権限で実行されます。サブスキルが処理する悪意あるドキュメントが、
正当なアクセス権のないクレデンシャルを読み取り、その内容を出力に含めるよう
サブスキルに指示する可能性があります。これは OS が攻撃者のために自身の権限を
悪用させられる古典的な **Confused Deputy** 攻撃です。

### `required_credentials` の宣言

サブスキルは `skill.md` フロントマターでクレデンシャルの必要性を宣言します：

```yaml
# skill.md
name: github_pr_reviewer
required_credentials:
  - github_token
  - atlassian_token
```

`required_credentials` が省略された場合のデフォルトは `["*"]` で、完全なクレデンシャル委譲を意味します。
FP-0016 以前に記述された既存スキルとの後方互換性を保つためです。

クレデンシャルが一切不要なスキルを明示的に宣言するには、空リストを使います：

```yaml
required_credentials: []
```

### `run_skill` によるスコープの絞り込み

`run_skill` の境界で、OS はサブスキルの `required_credentials` 宣言から
`ScopedSecretStore` を構築し、親のスコープ済みストアと交差（intersection）します。
サブスキルが親自身が保持しないクレデンシャルを取得することはできません：

```
親のスコープ: {"github_token", "stripe_key", "datadog_key"}
サブスキルの宣言: ["github_token", "slack_token"]
有効スコープ: {"github_token"}  ← 交差結果; slack_token は親になし
```

親ストアが非制限（`["*"]`）の場合は、サブスキルの宣言リストがそのまま採用されます
（交差は不要）。

### `CredentialScopeError`

サブスキルが有効な許可セット外のクレデンシャルを読み取ろうとすると、
`CredentialScopeError`（`PermissionError` のサブクラス）が発生します。
列挙操作もブロックされます。`list_visible_keys()` は許可かつ存在するキーのみを返し、
スコープ外のキーは「読めない」のではなく「見えない」状態になります。

```python
from reyn.secrets import ScopedSecretStore, CredentialScopeError

store = ScopedSecretStore(allowed_keys=["github_token"], path=secrets_path)
store.get("github_token")    # OK — 値を返す
store.get("stripe_key")      # CredentialScopeError を発生
"stripe_key" in store        # False — 例外なし、漏洩なし
store.list_visible_keys()    # ["github_token"] のみ
```

### 監査証跡

すべての `run_skill` 呼び出しは、その呼び出しで有効な許可キーセットを記録した
`sub_skill_credential_scope` P6 イベントを発行します：

```bash
grep '"sub_skill_credential_scope"' .reyn/events.jsonl
```

イベントペイロードには `skill`（サブスキル名）と `allowed_keys`
（ソート済みリスト、または非制限の場合 `["*"]`）が含まれます。
これにより、すべてのサブスキルへのクレデンシャル付与が監査可能かつリプレイ可能になります（P6）。

## パーミッションシステムではないもの

- **Linux ケイパビリティサンドボックスではありません。** `mode: unsafe` での Python ステップは同じユーザーとして実行されます。reyn はカーネルをサンドボックス化しません。
- **シークレットの保管庫ではありません。** 認証情報を approvals.yaml に入れたり、パーミッションで環境変数を隠そうとしないでください。認証情報には [コンセプト: シークレット管理](secret-handling.md) を使用してください。
- **ユーザーに対する保護ではありません。** `reyn.yaml` で `permissions: shell: allow` とした場合、シェルを承認したことになります。このシステムは意図せずケイパビリティが増大することを防ぐものであり、ユーザーの意図を防ぐものではありません。

## 参考

- [Reference: permissions](../reference/config/permissions.md) — 完全なスキーマ
- [Reference: reyn.yaml](../reference/config/reyn-yaml.md) — `permissions:` キーと `permissions.mcp_install`
- [Reference: state-dir](../reference/config/state-dir.md) — `.reyn/approvals.yaml`
- [コンセプト: シークレット管理](secret-handling.md) — 認証情報のストレージ（`~/.reyn/secrets.env`）
- [Reference: `reyn mcp`](../reference/cli/mcp.md) — `install` サブコマンドと `mcp_install` ゲートの連動
- [How-to: manage permissions](../guide/for-users/manage-permissions.md)
