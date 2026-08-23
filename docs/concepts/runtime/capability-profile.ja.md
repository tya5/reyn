---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [capability profile, agent profile, allowed_mcp, tool_allow, tool_deny, mcp_allow, mcp_deny, categories, category visibility, ContextualLayer, ProfileLayer, self-edit, untrusted narrowing]
---

# ケイパビリティプロファイル

ケイパビリティプロファイルシステムは、`mcp` / `tool` / `category` のケイパビリティ軸にわたる統合された絞り込みプリミティブです。**仕様**（何を絞り込むか）と**バインディング**（いつ・どのように適用するか）を分離します。

2 つのバインディングアダプターが 1 つのプリミティブを読み込みます。両方が同じ論理積 ∩ に入力します:

```
effective = AgentLayer ∩ SandboxLayer ∩ ProfileLayer ∩ ContextualLayer
```

2 アダプター設計の詳細は [パーミッションモデル § 1 仕様、2 バインディングアダプター](permission-model.md#effective-permission-conjunctive-restrict-model) を参照してください。

## 2 つのサーフェス、2 つのオペレーターファイル

### `AgentProfile` — `.reyn/agents/<name>/profile.yaml`

エージェントごとのアイデンティティとベースライン許可リスト。オペレーターは自然なキー名でこのファイルを書きます:

- `name`、`role`、`created_at` — アイデンティティ
- `allowed_mcp` — MCP サーバー許可リスト（内部的に `mcp_allow` にマップ）

`AgentProfile.default_profile()` はランタイムにこれらのキーを `CapabilityProfile` に変換します — ユーザー向けのリネームなし、セマンティクスは同じです。これが **ProfileLayer**（エージェントごとのデフォルトバインディング）に入力します。

フルスキーマ: profile.yaml を参照してください。

### `CapabilityProfile` — `.reyn/capability_profiles/<name>.yaml`

名前付きの宣言的ケイパビリティ仕様。1 つのプロジェクトに複数定義でき、実行中のセッションにはゼロまたは複数が同時に適用されます。これが合成を通じて **ContextualLayer**（セッションごとの動的バインディング）に入力します。

## `CapabilityProfile` 仕様

全フィールドはオプションです; 省略または `null` はその軸で無制限を意味します。

### 軸 A — MCP 絞り込み

| フィールド | 型 | セマンティクス |
|-----------|-----|--------------|
| `mcp_allow` | `list[str] \| null` | MCP サーバー許可リスト。`null` = 制約なし。 |
| `mcp_deny` | `list[str]` | MCP サーバー拒否リスト。 |

### 軸 B — ツール絞り込み

| フィールド | 型 | セマンティクス |
|-----------|-----|--------------|
| `tool_allow` | `list[str] \| null` | ツール許可リスト。`null` = 制約なし(拒否リストのみ)。 |
| `tool_deny` | `list[str]` | ツール拒否リスト。同名では拒否が許可より優先。 |

### 軸 C — カテゴリ可視性

| フィールド | 型 | セマンティクス |
|-----------|-----|--------------|
| `categories` | `list[str] \| null` | **可視のままにする**カテゴリ。`null` = 全て可視。`[]` = 全て非表示。 |

不明なカテゴリ名は no-op（前方互換）。`visible ⊆ authorized` は構造的に成立 — 可視性は非表示にできるだけで、再付与はできません。

### アイデンティティフィールド

| フィールド | 型 | デフォルト |
|-----------|-----|----------|
| `name` | string | 必須 (== ファイルステム) |
| `description` | string | `""` |

## 合成（ContextualLayer）

1 つのセッションで複数のプロファイルが適用される場合、`compose_resolved` は**最も制限的なものが勝つ**ルールで統合します:

- `*_deny` → **和集合**（いずれかのプロファイルの拒否が勝つ）
- `*_allow` → 全ての制約ある許可リストの**共通集合**（`null` = ⊤、スキップ）
- `excluded_categories` → **和集合**（いずれかのプロファイルの非表示が勝つ）

空のプロファイルリスト → 不活性な結果、プロファイルなしと同一。

## コンテキスト自動 untrusted 絞り込み（オプトイン、既定オフ）

アクティブなコンテキストにアンドラステッドな外部コンテンツがライブで存在する間、1 つのプロファイルが適用されます。**既定ではオフ**で、オペレーターが明示的に有効化したときにのみ働きます（#3501）:

**有効化:** `reyn.yaml` の `safety.threat_scan.capability_narrowing` — 1 本の順序付きラダー:

| 設定 | 挙動 |
|------|------|
| `off`（既定） | 絞り込みは一切かからない。コンテキストに何が入っても、エージェントはセッション開始時の能力を保ち続ける。 |
| `turn` | 外部コンテンツがライブな間 `_untrusted` プロファイルを適用。ターン境界で解決するので、ターン途中に届いたコンテンツは *次の* ターンから効く。 |
| `iteration` | `turn` に加えて router ループの反復ごとに再解決。ラウンド *N* で届いたコンテンツが同一ターンのラウンド *N+1* を絞り込む。ターン内では単調（compaction による taint の洗浄が効かない）。 |

**なぜ既定オフか。** この絞り込みはセッション途中で能力を取り上げ、取り上げられた側にはその理由が見えません。報告された症状は「セッション初めは使えていた機能が web fetch 後に使えなくなり、LLM がその理由を説明できなかった」というものでした。予測可能性を既定とし、セキュリティ強化はオプトインします。有効化した状態が実用に耐えるのは、拒否地点での legibility（下記）があるからです。

**プロファイル名:** `_untrusted`（ビルトインの拒否セット; `.reyn/capability_profiles/_untrusted.yaml` でオーバーライド可能）。設定は「絞り込みを走らせるか」を、プロファイルファイルは「走ったとき何を拒否するか」を決めます — 同じことの 2 通りの書き方ではありません（`delegation.capability_default` + `_delegate.yaml` と同じ分担）。

**トリガー（有効化後）:** メタに `external_source=true` を持つ任意の履歴/コンテキストエントリ（コンテンツフェンスのシームが取り込み時にスタンプ）。

**ビルトインの拒否セット:** メモリ書き込み/削除、再委譲、サンドボックス実行、MCP インストール。アンドラステッドなコンテンツは読み取りと推論が可能ですが、不可逆アクションを駆動できません。オーバーライドは意図的な緩和です — 不正な `_untrusted.yaml` はビルトインにフォールバックします（stderr に出力）。

## 拒否メッセージはどの絞り込みが効いたかを名指す

能力を絞り込みうる経路は 6 つあります — トポロジのバインディング、`_delegate` フロア、
セッション単位の capability config、⊆-親キャップ、`/visibility` オーバーライド、
`_untrusted` コンテキスト絞り込み。合成はこれらを 1 つの ∩ 項に畳み込むため、
畳み込まれた項だけでは「どれが拒否したのか」を答えられません。

そこで各項は `NarrowingOrigin` を持ちます — 3 つの部分すべてが必須です:

| 部分 | 答えるもの |
|------|-----------|
| `label` | どの絞り込みか（引ける名前で） |
| `cause` | なぜ今それが効いているのか |
| `lifts_when` | 何をすれば解けるのか（条件・設定キー・その両方） |

合成後の項は元の項を保持しているので、拒否地点はそれらを走査して最初に拒否した項の
origin を報告します。1 つの共有ビルダー（`contextual_deny_message`）が全拒否地点
（router ループの tool ゲート、`require_tool`、`require_mcp`）の文言を作るため、
同じ問いに対して 3 通りの情報量に分岐することがありません。

**これは LLM が受け取るメッセージそのもの**であり、Tool タブの表示だけではありません。
どれが効いたかを言わずに候補を並べる拒否は decision-enabling ではなく、その文字列を
受け取ったエージェントは損失を説明することも取り戻すこともできません。

## エージェントの自己編集

エージェントは追加のパーミッションを要求せずに、ランタイムにどちらのサーフェスも更新できます。両方のパスがデフォルトの書き込みゾーン（`.reyn/`）内にあり、保護されたパスではありません。

### コンテキスト仕様の編集

**パス:** `.reyn/capability_profiles/<name>.yaml`

**効果:** ContextualLayer 経由で適用; 複数プロファイル間で合成可能。

**手順:** 目的の軸を含む YAML を書き込みます。セッションごとのタスクスコープ絞り込みの ContextualLayer 入力として使用します。

### エージェントごとのベースライン編集

**パス:** `.reyn/agents/<agent_name>/profile.yaml`

**効果:** ProfileLayer 経由で適用（エージェントのデフォルト仕様）; 自然な `allowed_mcp` キーを使用（YAML リネームなし）。

**検証済み:** `_DEFAULT_WRITE_ZONES = (".reyn",)` であり、`_CANONICAL_PROTECTED_WRITE_PATHS` には `.reyn/approvals.yaml` と `.reyn/index/sources.yaml` のみが含まれます。`src/reyn/security/permissions/permissions.py` で確認済み。⚠️ **#5153以降stale**(既知のgap、#5173): ライブの承認ストアは `.reyn/approvals.jsonl` へ移りましたが、このリストはそれをcarve-outしていません。

## リロード

両サーフェスは**ターン境界でのホットリロード**をサポートしています（ライブ、再起動不要）:

- **ContextualLayer** — `.reyn/capability_profiles/<name>.yaml` の変更は `per_agent_capability` リアプライシームによって取得され、`AgentProfile` を再読み込みしてセッションが所有する 3 つのホルダー（session / skill_runner / router_host）の `allowed_mcp` を更新します。
- **ProfileLayer** — `.reyn/agents/<name>/profile.yaml` の変更も同じシームによってリロードされます。

両ファイルは IN-set（`.reyn/*.yaml` グレイン）です。`/reload` または `hooks_add` LLM-op でリロードをトリガーできます。完全なリロードサイクル（timing-B セーフポイント、適用前バリデーション、P6 イベント）については [コンセプト: Config ホットリロード](config-hot-reload.md) を参照してください。

ペーエージェントフックレイヤー（`.reyn/agents/<name>/hooks.yaml`）も、`hooks` リアプライシームを経由して同じターン境界でリロードされます — `hooks` COMBINE はリロードのたびに startup + runtime + per-agent レイヤーを再読み込みします。

## スキーマ例

```yaml
# .reyn/capability_profiles/read-only-researcher.yaml
name: read-only-researcher
description: "読み取りと推論のみ; 書き込み・委譲・実行なし。"
categories:            # 可視のままにする
  - file
  - web
mcp_allow: null        # 全 MCP サーバー利用可能
mcp_deny: []
tool_allow: null       # 拒否リストのみ
tool_deny:
  - exec
  - remember_shared
  - run_prompt
  - send_to_session
```

**各ツールは invocable な名前を 1 つだけ持ちます。** `#3429` が、かつて全アクションが
併せ持っていた第二の catalog-qualified な綴り（例: `read_file` と並んで存在した旧
`file__read`）を削除しました — `tool_deny` のエントリは現在、それが名指す名前だけを
展開なしで deny します。モデルの現在のツール一覧から名前を写せば安全かつ十分です:
別綴りで同じ操作に到達する経路はもう存在しません。

## 参照

- [パーミッションモデル § 論理積制限 + 1 仕様 2 バインディングアダプター](permission-model.md#effective-permission-conjunctive-restrict-model) — ∩ 式、ProfileLayer vs ContextualLayer、アダプター設計
- [コンセプト: マルチエージェント](../multi-agent/multi-agent.md) — トポロジーと委譲（ContextualLayer コンシューマー）
- [リファレンス: reyn agent CLI](../../reference/cli/agent.md) — `reyn agent new`、`reyn agent list`
