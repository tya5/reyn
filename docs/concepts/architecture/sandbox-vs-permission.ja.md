---
type: concept
topic: architecture
audience: [human, agent]
---

# サンドボックスとパーミッション：直交する関心事

Reyn には、ワークフローができることをゲートする 2 つの独立したシステムがあります。これらは**完全に直交しています**——それぞれ異なる問いに答え、異なるレベルで設定されます。両者を混同することはよくある混乱の原因です。

## パーミッション：この agent/session はこの能力を使えるか？

`permissions:`（`reyn.yaml` の `permissions:` ブロックで宣言 — [リファレンス](../../reference/config/permissions.md)）は有効な**アクセスポリシー**を記述します：

- どのファイルパスを読み書きできるか？
- ネットワークリクエストを発行できるか？どのホストに対して？
- シェルコマンドや MCP ツールを呼び出せるか？

パーミッションは**ワークフローレベルではなくプロジェクトレベル**です：`skill.md` フロントマターに `permissions:` キーはありません — このページ（および `reference/config/permissions.md`）の以前のバージョンはそのキーを記述していましたが、それを読む parser は一度も実装されませんでした（#5863）。プロジェクトの `reyn.yaml` をオペレーターが宣言し、その宣言が cover しないアクセスは just-in-time プロンプトで補います。ランタイムは宣言された集合を[論理積パーミッションモデル](../runtime/permission-model.md#effective-permission-conjunctive-restrict-model)の AgentLayer を通じて強制します。

```yaml
# reyn.yaml
permissions:
  file.write:
    - path: "{{workspace}}/output"
      scope: recursive
  http.get:
    - host: "api.github.com"
```

**設定者：** オペレーターが `reyn.yaml` で宣言し、JIT プロンプトはオペレーター / ユーザーが承認。
**答える問い：** 「この op は許可されているか？」

## サンドボックス：ワークフローはどのように閉じ込められているか？

`sandbox`（`reyn.yaml` の `sandbox:` 以下、または CLI フラグで設定）は agent の**封じ込め**モデルを記述します：

- どのバックエンドが分離を強制するか（Seatbelt / Landlock / コンテナ / なし）？
- どのコンテナイメージを使うか？
- どのファイルシステムマウントやネットワーク制限を適用するか？

サンドボックスは**agent レベル**です：ワークフローごと・フェーズごとではなく、1 つのサンドボックス設定が agent 全体に適用されます。これはワークフロー作成者が宣言するものではなく、オペレーターのデプロイ設定の一部です。

```yaml
# reyn.yaml
sandbox:
  backend: auto     # auto | seatbelt | landlock | noop
```

**設定者：** オペレーター。
**答える問い：** 「ワークフローを実行するプロセスはどのように閉じ込められているか？」

## 組み合わせ方

パーミッションとサンドボックスは独立して論理積で適用されます：

```
allowed = permission_check(op) AND sandbox_check(backend, op)
```

パーミッションシステムが許可する op でも、サンドボックスが拒否することがあります——たとえば `reyn.yaml` で許可した `http.get: [{host: "api.github.com"}]` パーミッションでも、`network: false` サンドボックスポリシー下ではサンドボックスレイヤーで拒否されます。`permissions:` の許可はサンドボックス設定を上書きできません。

逆に、サンドボックスがパーミッションシステムの拒否するものを許可することもあります——たとえば広いサンドボックス設定があっても、`reyn.yaml` の `permissions:` ブロックが宣言していないシェル op へのパーミッションを与えることにはなりません。

## まとめ

| 軸 | パーミッション | サンドボックス |
|---|---|---|
| レベル | プロジェクトレベル（`reyn.yaml` ごと） | agent レベル |
| 宣言者 | オペレーター | オペレーター |
| 承認者 | ユーザー / オペレーター（JIT プロンプト） | オペレーター（設定 / CLI） |
| 対象 | op アクセスポリシー（このプロジェクトの agent は何をできるか？） | 封じ込め（プロセスはどのように分離されるか？） |
| 定義場所 | `reyn.yaml` の `permissions:` | `reyn.yaml` の `sandbox:` / CLI |

## 参照

- [Permission model](../runtime/permission-model.md) — 認可レイヤー、論理積制限モデル、保護された書き込みパス
