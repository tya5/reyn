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

## 宣言軸の taxonomy（= bool flag と resource list の使い分け）

`skill.md` frontmatter の `permissions:` ブロックは **3 種類の軸 shape** を混在させている。 本セクションは各軸を明示的に列挙し、 新軸追加時の shape 選定 criterion を確立する。

### 現状の軸

| 軸 | 型 | 粒度 | gate site | 補足 |
|---|---|---|---|---|
| `file.read` | `list[{path, scope}]` | resource（per-path）| `require_file_read()` | scope ∈ {`just_path`, `recursive`} |
| `file.write` | `list[{path, scope}]` | resource（per-path）| `require_file_write()` | write / edit / delete を包含 |
| `python` | `list[{module, function, mode, timeout}]` | resource（per-step）| `require_python_step()` | mode ∈ {`safe`, `unsafe`} |
| `mcp` | `list[str]` | resource（per-server）| MCP 呼び出し時 implicit | サーバー名の allowlist |
| `tool` | `list[str]` | resource（per-tool）| `require_tool()` | 名前指定 tool allowlist |
| `shell` | `bool` | abstract | `require_shell()` | binary（= shell 全般へのアクセス）|
| `mcp_install` | `bool`（宣言）+ per-server approval key | hybrid | `require_mcp_install()` | 宣言は bool / approval は `<server_id>` キーで永続 |
| `mcp_drop_server` | `bool`（同 shape）| hybrid | `require_mcp_drop_server()` | `mcp_install` の counter-op |
| `index_drop` | `bool`（同 shape）| hybrid | `require_index_drop()` | RAG corpus / source の drop |
| `cron_register` | `bool`（同 shape、 per-job approval）| hybrid | `require_cron_register()` | register / unregister / enable / disable をまとめて |
| `allowed_mcp` | `list[str]` または `None` | ACL filter | MCP 呼び出し時 implicit | per-agent restriction、 `mcp` 軸とは cross-cut |

### Criterion — `bool` 軸 vs `list` 軸

ある capability が **bool 軸** に属するのは、 **以下を全て満たす** とき:

1. その capability が起こす **side-effect 集合** が単一 resource scope で表現できない（= config write + chain notify + state_change emit のような複合効果、 `mcp_install` triad が典型）。
2. Skill 作者が write-time に対象 instance を列挙できない（= 「どの server を install するか」 は runtime に user / LLM が決める、 skill 作者には不可知）。
3. user が runtime に **per-instance** 承認面を見たい — 宣言は intent shape（= bool）でも、 approval は resource-keyed という hybrid shape。

ある capability が **list 軸** に属するのは、 **以下を全て満たす** とき:

1. 単一の I/O scope に reducible（= 1 path / 1 host / 1 server）。
2. Skill 作者が write-time に inventory を知っている（= 「これらの path を読む」）。
3. runtime per-instance prompt は不要（= list 自体が scope、 config tier が ask しない限り追加 prompt なし）。

**`allowed_mcp` ACL 軸** は 3 つめの shape — capability を grant せず、 既に grant 済の resource list の **subset を agent ごとに restrict** する。 ACL filter は bool / list 両軸を cross-cut する別系統。

例示:

| Capability | shape | 理由 |
|---|---|---|
| `file.write` | list（resource）| 単一 I/O scope（= 1 path）、 作者が inventory を知る、 chain effect なし |
| `mcp_install` | bool（hybrid）| side-effect 集合（= config write + emit + notify）、 server name は runtime 決定、 user が per-server prompt 希望 |
| `shell` | bool | side-effect 集合 unbounded（= 任意プロセス実行）、 作者は 「これらの command」 と列挙不可 |
| `cron_register` | bool（hybrid）| side-effect 集合（= cron.yaml write + emit）、 job name は runtime 決定、 per-job approval key |

### 隠れた軸 — 「intent ↔ raw I/O」 correlation

bool 軸は **intent** を宣言するが、 実際の side-effect（= file write / HTTP / process spawn）は **underlying primitive** を通る。 同じ raw I/O が直接到達可能（= `file.write` の default-zone path 経由）な場合、 bool 軸の intent は **bypass 可能**。

具体例: `mcp_install: true` は 「この skill は MCP server を install する」 と宣言するが、 副作用（= `.reyn/mcp.yaml` への write）は `reyn.safe.file.write(".reyn/mcp.yaml", ...)` でも到達可能 — `.reyn/` が default-zone write path に含まれるため。 2 経路の reconcile が無い — 後述 [Known gaps](#known-gaps-2026-05-23-監査-追跡は個別-pr) 参照。

提案する canonical correlation rule（= 未実装）:

> bool 軸 B が記述する side-effect 集合に raw I/O R が含まれる場合、 R をどの経路（宣言済 list / default zone / OS 内部 code 経由）で実行しても B の gate を通過する必要がある。

現実装は 4 つの bool 軸（`mcp_install` / `mcp_drop_server` / `index_drop` / `cron_register`）いずれもこの rule を強制していない。

## Trust boundary レイヤー（= 強制が実際どこで掛かるか）

side-effect を実行する 4 つの surface を強制力の強い順に:

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
│  reyn パッケージ内部（OS handler / registry client）          │  ← 最弱
│    permission_resolver は call path に居ない                  │
│    Trust boundary: このコードは OS 自身、 caller を gate するが │
│    自分自身は gate されない                                   │
└──────────────────────────────────────────────────────────────┘
```

最上層・最下層は意図的な非対称、 中 2 層は現状制限:

- **最上層 (sandboxed_exec)** は OS-kernel 強制を持つ唯一のレイヤー。 argv / network / fs scope を per-call で declarative 宣言、 platform sandbox が強制。
- **最下層 (reyn パッケージ内部)** は構造的に trust 内側 — OS が caller を gate、 自分自身は gate しない。 ここに gate を足すには (a) OS 内部の permission model 別建て（= 循環）、 もしくは (b) I/O を `op_runtime` 層に移す（= 既に gate がある層）必要があり、 どちらも architectural commitment が大きい。
- **safe-mode python** は honor system: AST validation が `import os` を弾き、 `reyn.safe.file` が宣言済 path を check。 motivated な user が `mode: unsafe` で bypass 可、 通常の `mode: safe` author が accidentally bypass はしにくい。
- **unsafe-mode python** は宣言 trust: operator が runtime に `--allow-unsafe-python` を承認、 step が host へのフルアクセスを持つことを accept。

## 業界比較（= 設計選択の参照）

| Platform | 宣言 shape | runtime ask | 粒度 | 強制レイヤー |
|---|---|---|---|---|
| iOS (TCC + Entitlements) | `Info.plist` capability + purpose string | First-use prompt | Capability axis | OS kernel + signed entitlements |
| Android (≥ M) | `AndroidManifest.xml` `uses-permission` | dangerous tier に first-use prompt | Permission class + scoped storage | OS kernel + per-app UID |
| Web Permissions API | feature ごとに query | per-permission prompt | **Origin-scoped**（= per-domain capability）| Browser sandbox |
| Anthropic Claude Code | Tool list（Bash / Edit / Read / Write）| デフォルトでは無、 sandbox-mode で opt-in | Tool 名（path scope なし）| Seatbelt（sandbox-mode）または trust |
| MCP servers | server side で tool list 公開 | server がオーナーシップ | per-tool、 server 定義 | プロセス境界 |
| **Reyn** | `permissions:` ブロック（本 doc）| startup_guard + first-use interactive | **Hybrid**（= per-path list + abstract bool）| safe-mode は AST + honor system / `sandboxed_exec` のみ kernel |

業界の主流は **abstract capability 宣言 + runtime per-instance prompt + OS-kernel 強制**。 Reyn は 2 軸で乖離:

1. **粒度は業界標準より細かい** — path-list `file.read` / `file.write` は iOS / Android の capability axis より Web の origin-scope に近い。 正当化: Reyn skill は workflow code（= 作者が file inventory を知る）、 iOS / Android app は general-purpose（= 作者が write-time に列挙不可）。
2. **safe-mode python の強制は honor system** — iOS / Android は kernel boundary、 Reyn は AST + path-list check。 Trade-off: 実装簡素（= per-step seatbelt セットアップ不要） vs 強制の弱さ。

これらの乖離は明示的な design choice だが、 safe-mode-python honor-system contract に制約を課す: `reyn.safe.*` を bypass する経路（= AST hole / 直接 I/O path で AST が捕捉しないもの）は宣言 path check を silently escape する。 上の criterion section で挙げた bool 軸 cross-cutting correlation rule は、 この honor-system 破綻が最も visible に現れる surface である。

## Known gaps (2026-05-23 監査、 追跡は個別 PR)

2026-05-23 軸 taxonomy 監査で identify した 3 つの architectural inconsistency。 個別 PR で remediation する前提で、 gap を明示する目的でここに記録。

### Gap A — bool intent vs raw I/O default-zone bypass

4 つの bool 軸（`mcp_install` / `mcp_drop_server` / `index_drop` / `cron_register`）は **intent** を宣言するが、 その side-effect 集合（= `.reyn/` 配下への config write）は `reyn.safe.file.write()` 経由でも到達可能 — `.reyn/` が default-zone write path のため（= `src/reyn/kernel/preprocessor_executor.py:493-499`）。

具体的 bypass: safe-mode python step は `.reyn/mcp.yaml` を直接 write して MCP server registry を変更できる、 `mcp_install: true` 宣言なし + `require_mcp_install()` 承認 prompt なしで。

Remediation 候補（= 後続 PR）:

- Cross-axis correlation gate: `_check_write(path)` で 「raw I/O → bool 軸」 registry（= `.reyn/mcp.yaml` → `mcp_install` / `.reyn/cron.yaml` → `cron_register` 等）を consult、 path match 時に bool 軸も追加強制。
- もしくは canonical な MCP registry mutation を `safe.file` 経由から外す（= `op_runtime/mcp_install.py` 内のみに keep、 safe.file 層で直接 write を refuse）。

cross-cutting rule は上の [宣言軸の taxonomy](#宣言軸の-taxonomy-bool-flag-と-resource-list-の使い分け) section に articulate 済、 現実装はこれを強制していない。

### Gap B — reyn パッケージ内部コードが permission resolver を経由しない

OS 内部 code（= `src/reyn/registry/client.py` の MCP registry HTTP、 `src/reyn/op_runtime/mcp_install.py` の config write）は `PermissionResolver` を consult せず I/O を実行する。 これは意図的な trust boundary（= OS が caller を gate、 自分自身は gate しない）だが、 境界が undocumented。

この gap は **必ずしも bug ではない**: OS 内部 code への厳密な gate は (a) OS 内部の permission model 別建て（= 循環）か (b) I/O を `op_runtime` 層に押し出す（= 既に gate がある）かのいずれかが必要で、 どちらも architectural commitment が大きい。

Disposition: known trust-boundary choice として記録。 具体的な OS 内部 I/O path が user-visible になり operator が gate を要求する場合（= 例: MCP registry call の `web.fetch` event ログ化）に re-open。

### Gap C — `safe.http` は存在するが gate されていない

`src/reyn/safe/http.py`（= FP-0042 Phase 3 drift-fix で landed）は `get` / `post` / `put` / `delete` を ship — urllib-backed、 AST allowlist 経由で safe-mode step から呼び出し可。 しかし **per-call permission gate を持たない**: module の docstring 自体が 「ここでの "safe" は namespace 役割（= AST-allowlisted）であって、 `reyn.safe.file` が enforce する per-call permission-resolver pattern ではない」 と明示し、 設計解決先として本 issue を参照している。

現状の stdlib 使用:

- `mcp_search` / `mcp_install` は domain-specific `reyn.safe.mcp.registry`（= hardcoded registry URL、 host は skill から見えない）を使用。
- `skill_search` / `skill_importer` は bare `reyn.safe.http.get`（= 任意 URL、 host check なし）を使用。

宣言 shape の design question（= gate を追加するときの形）は依然として:

- **per-host allowlist**（= `http.get: [{host: "registry.modelcontextprotocol.io"}]`）— resource-list criterion 合致（= 単一 I/O scope、 作者が inventory を知る）。
- **bool flag**（= `http: true`）— bool criterion 合致は HTTP が fetch 自体を超える side-effect を持つ場合のみ（= 定義上 HTTP GET は read-only でそれを持たない）。
- **method + origin-scope**（= Web Permissions API style）— 業界 pattern に最も近く、 Reyn の workflow-code use case に granular 十分。

上の criterion section の規則によれば: HTTP read は単一 I/O scope、 作者が host を write-time に知る、 fetch 以外の side-effect なし → **resource list、 bool ではない**。 現在の un-gated state が gap、 `safe.http` に per-host allowlist を追加するのが整合した remediation。

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
