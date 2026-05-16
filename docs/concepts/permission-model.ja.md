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
┌──────────────────────────────┐  Phase frontmatter で宣言。ユーザーが承認
│  Phase 宣言                  │  承認は .reyn/approvals.yaml に永続化
└──────────────────────────────┘
             ↓ プロジェクトを広く信頼する場合
┌──────────────────────────────┐  reyn.yaml: permissions.<key>: allow
│  プロジェクト全体の事前承認    │  そのケイパビリティのプロンプトをバイパス
└──────────────────────────────┘
```

### レイヤー 1：デフォルト

プロジェクトルート配下のどこでも読み取り/glob/grep。書き込み/編集/削除は `.reyn/` または `reyn/` 配下のみ。シェル、MCP、Python は不可。

### レイヤー 2：Phase 宣言

デフォルト外のものが必要な Phase は frontmatter でそれを宣言します。Skill の起動時、ランタイムは単一の承認プロンプトを表示します：

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

## `python` パーミッションと `mode: safe` allowlist

`python` パーミッションには 2 段階あります:

| 段階 | 設定キー | 許可する内容 |
|-------|-----------|----------------|
| `safe` | `python.pure: allow`（旧キー） | `PURE_STDLIB_ALLOWLIST` に含まれるモジュールのみ import 可能なステップ — clock、entropy、純粋計算、および `__future__`（コンパイラディレクティブ）。ファイルシステム・ネットワーク・プロセスへのアクセス不可。 |
| `unsafe` | `python.trusted: allow` | ファイルシステム・ネットワークを含む任意モジュールの import が可能なステップ。 |

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
