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
    pure: allow
    trusted: allow
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

## パーミッションシステムではないもの

- **Linux ケイパビリティサンドボックスではありません。** `mode: trusted` での Python ステップは同じユーザーとして実行されます。reyn はカーネルをサンドボックス化しません。
- **シークレットの保管庫ではありません。** 認証情報を approvals.yaml に入れたり、パーミッションで環境変数を隠そうとしないでください。認証情報には [コンセプト: シークレット管理](secret-handling.md) を使用してください。
- **ユーザーに対する保護ではありません。** `reyn.yaml` で `permissions: shell: allow` とした場合、シェルを承認したことになります。このシステムは意図せずケイパビリティが増大することを防ぐものであり、ユーザーの意図を防ぐものではありません。

## 参考

- [Reference: permissions](../reference/config/permissions.md) — 完全なスキーマ
- [Reference: reyn.yaml](../reference/config/reyn-yaml.md) — `permissions:` キーと `permissions.mcp_install`
- [Reference: state-dir](../reference/config/state-dir.md) — `.reyn/approvals.yaml`
- [コンセプト: シークレット管理](secret-handling.md) — 認証情報のストレージ（`~/.reyn/secrets.env`）
- [Reference: `reyn mcp`](../reference/cli/mcp.md) — `install` サブコマンドと `mcp_install` ゲートの連動
- [How-to: manage permissions](../guide/for-users/manage-permissions.md)
