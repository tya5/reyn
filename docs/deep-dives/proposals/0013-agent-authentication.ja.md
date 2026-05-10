# FP-0013: エージェント認証 — OAuth 委譲・トークンライフサイクル・MCP 認証ヘッダー

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Reyn は現在 `secrets.store` での静的 API キーのみをサポートしている。これにより、拡大する
HTTP モード MCP エコシステムへの接続がブロックされ、OAuth トークンの期限切れによって長時間
タスク中に 401 エラーが発生し、エージェントごとの認証情報使用の監査証跡も存在しない。
本提案では 5 つのコンポーネント — MCP Bearer ヘッダー・OAuth トークンリフレッシュ・
デバイス認証グラント ログイン・スキルごとの認証情報スコーピング・エージェント ID 伝播 —
を導入し、Reyn が認証をユーザー設定の環境変数ではなく OS の第一級の関心事として扱えるようにする。

---

## Motivation

実践者の声（Reddit・HN・Zenn・Qiita）のサーベイでは、認証はコミュニティを横断する繰り返しの
ギャップとして浮上しているが、障害の形はエンジニアリングのコンテキストによって異なる。

### シナリオ 1 — MCP HTTP Bearer ヘッダー（即時のブロッカー）

HTTP トランスポート型 MCP サーバー（GitHub MCP・Atlassian MCP・Slack MCP・社内エンタープライズ
MCP サーバー）は各リクエストに `Authorization: Bearer <token>` を要求する。Reyn の
`mcp.servers.<name>` 設定には `headers:` フィールドがない。`secrets.store` の静的 API キーは
MCP 接続レイヤーから切り離されており、接続時にインジェクトする機構がない。

結果: HTTP モードの MCP サーバーはすべて、オペレーターが HTTP クライアントを手動でパッチしない
限り現在は到達不能である。`mcp_install` パーミッションゲート（ADR-0029）は完全に機能しているが、
接続自体が Bearer ヘッダーの段階で失敗する。

### シナリオ 2 — 長時間タスク中のトークン自動リフレッシュ

OAuth アクセストークンは約 1 時間（RFC 6749 §4.1）で期限切れになる。長時間動作するスキル
（特に FP-0012 の非同期実行で 30〜90 分に及ぶもの）はタスクの途中で 401 エラーに遭遇する。
`secrets.store` は静的な値のみを保存し、リフレッシュトークンのライフサイクルを持たない。
エラーは実行中スキルの奥深くで不透明なツール失敗として表面化し、リカバリーパスがない。

### シナリオ 3 — OAuth デバイス認証グラント（RFC 8628）

多くのエンタープライズ GitHub/GitLab/Azure DevOps 環境はポリシーにより個人アクセストークン
（PAT）を禁止し、OAuth フローを要求する。標準のブラウザリダイレクトフロー（認可コードグラント）
はヘッドレスまたは自律型エージェントには機能しない — リダイレクト先のブラウザが存在しないからだ。
デバイス認証グラント（RFC 8628）はこれを解決する: エージェントが URL とユーザーコードを表示し、
オペレーターが任意のデバイスで承認し、エージェントがトークンをポーリングで取得する。Reyn には
`reyn auth` CLI エントリーポイントも、このグラントタイプの第一級フローも存在しない。

### シナリオ 4 — サブスキルへのスコープ付き認証情報委譲

親スキルが `run_skill` 経由でサブスキルを起動する際、サブスキルは現在 `secrets.store` 全体を
継承する。処理対象ドキュメント内のプロンプトインジェクション攻撃がサブスキルに対して全認証情報の
持ち出しを指示できる（Confused Deputy 問題）。攻撃対象領域は `secrets.store` に追加される認証情報
ごとに拡大する。これは設定上の問題ではなく、現在の委譲モデルの構造的な脆弱性である。

### シナリオ 5 — エンタープライズ エージェント ID（Entra Agent ID パターン）

日本のエンタープライズ展開では、RBAC と監査証跡のためにエージェントごとの ID が必要とされる。
SOC2/ISO27001 のコンプライアンスは「誰が（どのエージェントが）何にアクセスしたか」の証明を
義務付ける。現在の P6 イベントには `agent_id` フィールドがない。サブスキルによる API 呼び出しは
親セッションからの呼び出しと区別できない。METI AI ガバナンス v1.1 は自動化システムのアクション
をアクターレベルで監査可能にすることを要求する。

### Reyn の差別化ポイント

ほとんどのエージェントフレームワークは認証を「ユーザーの問題 — 環境変数を設定してください」
として扱う。Reyn のパーミッションモデルと P6 監査証跡は、エージェント認証を OS の第一級の関心事
にする: 認証情報は必要なスキルにスコープ化され、ライフサイクルはランタイムが管理し、すべての
使用は追記専用のイベントログに記録される。これは ad-hoc な環境変数パターンでは現在対応不可能な
エンタープライズコンプライアンス要件に直接応える。

---

## Proposed implementation

### コンポーネント A — `mcp.servers.<name>.headers` 設定フィールド（SMALL）

`src/reyn/config.py` の `MCPServerConfig` にオプションの `headers: dict[str, str]` フィールドを
追加する。`src/reyn/mcp/client.py` の MCP HTTP クライアントがこの辞書を読み取り、接続時に渡す。
ヘッダー値は `${secret:my_token}` 補間（既存の環境変数インジェクションと同じパターン）で
`secrets.store` キーを参照できる。

```yaml
# reyn.yaml
mcp:
  servers:
    github:
      transport: http
      url: https://api.githubcopilot.com/mcp/
      headers:
        Authorization: "Bearer ${secret:github_token}"
```

OS レベルのポリシー変更は不要 — パーミッションシステム（ADR-0029）がすでに MCP サーバー接続を
ゲートしている。このコンポーネントは、すでにゲートされた接続を実際に機能させる。

対象ファイル:
- `src/reyn/config.py` — `MCPServerConfig.headers: dict[str, str] = {}`
- `src/reyn/mcp/client.py` — HTTP セッション作成時に `headers` 辞書を渡す

### コンポーネント B — `secrets.store` の OAuth トークン型 + リフレッシュライフサイクル（MEDIUM）

`secrets.store` に `OAuthToken` 認証情報型を追加する:

```python
@dataclass
class OAuthToken:
    access_token: str
    refresh_token: str
    token_uri: str
    client_id: str
    client_secret: str   # または PKCE — クライアントシークレット不要
    expires_at: datetime
    scopes: list[str]
```

ストアは `get_valid_token(key: str) -> str` メソッドを公開し、以下を行う:
1. `expires_at` が 60 秒以上先であれば `access_token` を返す
2. そうでなければ `grant_type=refresh_token` でトークンエンドポイントを呼び出す
3. 保存されたトークンを更新し、`token_refreshed` イベントを発行する（P6）
4. 新しい `access_token` を返す

コンポーネント A の `${secret:key}` 補間は、シークレット型が `OAuthToken` の場合に
`get_valid_token` を呼び出し、MCP 接続に対してリフレッシュを透過的にする。

対象ファイル:
- `src/reyn/secrets/store.py` — `OAuthToken` データクラス + `get_valid_token` メソッド
- `src/reyn/events/events.py` — `token_refreshed` イベントペイロード

### コンポーネント C — `reyn auth login <service>` CLI（デバイス認証グラント）（MEDIUM）

RFC 8628 デバイス認証グラントを実装する新しい `reyn auth` コマンドグループ:

```
reyn auth login github        → GitHub の Device Grant フローを開始
reyn auth login <custom_url>  → 汎用 OIDC エンドポイント
reyn auth list                → 保存済み OAuth トークン一覧（名前・スコープ・有効期限）
reyn auth revoke <service>    → secrets.store からトークンを削除
```

フロー:
1. デバイス認証エンドポイントへ POST → `device_code`・`user_code`・`verification_uri` を受信
2. `verification_uri` と `user_code` をターミナルに表示
3. 指数バックオフでトークンエンドポイントをポーリング（ステップ 1 の `interval` を遵守）
4. 成功時: `OAuthToken` を `<service>_token` キーで `secrets.store` に保存

これにより `reyn auth login github` が GitHub MCP のための標準オンボーディングパスになり、
現在の手動 PAT コピー貼り付けワークフローを置き換える。

対象ファイル:
- `src/reyn/cli/auth.py` — 新規コマンドグループ
- `src/reyn/cli/main.py` — `auth` グループを登録

### コンポーネント D — `run_skill` 起動時のスキルごとの認証情報スコーピング（LARGE）

スキルは `skill.md` のフロントマターで必要な認証情報を宣言する:

```yaml
required_credentials:
  - github_token
  - atlassian_token
```

OS が `run_skill` Control IR op でサブスキルを起動する際、宣言された認証情報のみを含む
`ScopedSecretStore` を構築してサブスキルのランタイムコンテキストに渡す。サブスキルは親
セッションが保持している内容に関わらず、このスコープ付きストア外の認証情報にアクセスできない。

これは P4（OS が LLM/スキルに必要な候補のみを提供する）と P5（ワークスペースが唯一の
信実の源 — スコープ付きストアがサブスキルの認証情報ワークスペースである）に整合する。

親スキルは `required_credentials: [*]` を宣言することで完全委譲を明示的にオプトインできる
（信頼された内部スキルのみ; P6 イベントで監査可能）。

対象ファイル:
- `src/reyn/op_runtime/run_skill.py` — 起動時に `ScopedSecretStore` を構築
- `src/reyn/secrets/store.py` — `ScopedSecretStore` ラッパー
- `src/stdlib/skills/*/skill.md` — stdlib スキルに `required_credentials` を追加

### コンポーネント E — P6 イベントおよびヘッダーへの `agent_id` 伝播（SMALL）

`reyn.yaml` に `agent_id: str` を追加する（オプション; デフォルトは `reyn/<hostname>`）。
この値はすべての P6 イベントペイロードに含まれ、外部 HTTP 呼び出し（MCP・A2A・外部 API）に
クライアントヘッダー（`X-Reyn-Agent-Id: <agent_id>`）としてインジェクトされる。

```yaml
# reyn.yaml
agent:
  id: "reyn/acme-corp/code-review-agent"
```

これにより、すべてのアクションがエージェントレベルで監査可能になり、SOC2/ISO27001 および
METI v1.1 の監査要件を満たす。

対象ファイル:
- `src/reyn/config.py` — `AgentConfig.id` フィールド
- `src/reyn/events/events.py` — ベースイベントペイロードの `agent_id`
- `src/reyn/mcp/client.py` — `X-Reyn-Agent-Id` ヘッダーのインジェクト

---

## 優先順位

**A → B → C → E → D**

コンポーネント A が即時のアンロッカーである: HTTP モードの MCP エコシステム全体をアクセス可能に
する。コンポーネント B は長時間タスク（FP-0012）がトークン期限切れを乗り越えられるようにする。
コンポーネント C は B が初回トークン取得に依存するエンタープライズ対応のオンボーディングフローを
提供する。コンポーネント E は小規模かつコンプライアンス上の価値が高い。コンポーネント D
（認証情報スコーピング）は最大かつ最も影響範囲が大きいが、セキュリティ上も最も重要 —
A〜C が安定した後に着手すべきである。

---

## Dependencies

- **FP-0012**（非同期スキル実行）: コンポーネント B（トークンリフレッシュ）は、複数のトークン
  有効期限ウィンドウにまたがる長時間非同期タスクにとって最も重要
- **ADR-0029**（mcp_install パーミッションゲート）: コンポーネント A は同じパーミッション
  強制パターンを使用; ゲートはすでに存在する
- RFC 8628（デバイス認証グラント）— コンポーネント C; Reyn 依存なし
- RFC 6749 §6（トークンリフレッシュ）— コンポーネント B; Reyn 依存なし

---

## Cost estimate

**合計: LARGE**

| コンポーネント | コスト | 備考 |
|---|---|---|
| A: `mcp.servers.headers` 設定フィールド | SMALL | 設定構造体 + HTTP クライアント; 約 50 行 |
| B: `OAuthToken` + リフレッシュライフサイクル | MEDIUM | 新規認証情報型 + P6 イベント |
| C: `reyn auth login` CLI（Device Grant） | MEDIUM | 新規 CLI コマンドグループ; RFC 8628 ポーリング |
| D: スキルごとの認証情報スコーピング | LARGE | スコープ付きストア + stdlib `skill.md` 全更新 |
| E: イベント・ヘッダーへの `agent_id` | SMALL | 設定フィールド + ベースイベントペイロード |
| テスト | MEDIUM | Tier 1: トークンリフレッシュ契約; Tier 2: スコープ付きストア分離 |

---

## Related

- `src/reyn/config.py` — `MCPServerConfig`・`AgentConfig`
- `src/reyn/mcp/client.py` — HTTP セッション作成
- `src/reyn/secrets/store.py` — 認証情報ストア
- `src/reyn/cli/auth.py` — 新規ファイル（コンポーネント C）
- `src/reyn/op_runtime/run_skill.py` — 起動時の認証情報スコーピング（コンポーネント D）
- `src/reyn/events/events.py` — `token_refreshed` イベント・`agent_id` ベースペイロード
- ADR-0029 — MCP インストール パーミッションゲート
- FP-0012 (`0012-async-skill-execution.md`) — 非同期実行; コンポーネント B の依存先
- RFC 8628 — デバイス認証グラント
- RFC 6749 §6 — OAuth 2.0 トークンリフレッシュ
