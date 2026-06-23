---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [capability profile, agent profile, allowed_skills, allowed_mcp, tool_allow, tool_deny, categories, category visibility, self-edit, named capability, untrusted narrowing]
---

# ケイパビリティプロファイル

ケイパビリティプロファイルシステムには、補完的な役割を持つ 2 つの異なるサーフェスがあります: エージェントごとのアイデンティティファイル（`profile.yaml`）と名前付きケイパビリティ仕様（`capability_profiles/<name>.yaml`）。

## 2 つのサーフェス

### `profile.yaml` — エージェントアイデンティティ（`AgentProfile`）

`.reyn/agents/<name>/profile.yaml` に格納。セッション構築時に読み込まれます。エージェントのアイデンティティと粗粒度の許可リストを持ちます:

- `name`、`role`、`created_at` — アイデンティティ
- `allowed_skills` — スキル許可リスト（ルーターがこのエージェントに提示するスキル）
- `allowed_mcp` — MCP サーバー許可リスト（このエージェントが呼び出せるサーバー）

これら 2 つの許可リストはランタイム ∩ ゲートに **ProfileLayer** として参加します:
`effective = AgentLayer ∩ SandboxLayer ∩ ProfileLayer`。

フルスキーマ: [profile.yaml リファレンス](../../reference/dsl/profile-yaml.md)。

### `capability_profiles/<name>.yaml` — 名前付きケイパビリティ仕様（`CapabilityProfile`）

`.reyn/capability_profiles/<name>.yaml` に格納。ツールレベルのケイパビリティの名前付き宣言的絞り込みです。1 つのプロジェクトに複数のプロファイルを定義でき、実行中のエージェントには 1 つ以上が同時に適用される場合があります。

これは #1827 で導入され、そのステージングアークを通じて拡張されるサーフェスです。

## `CapabilityProfile` の軸

ケイパビリティプロファイルは 2 つの独立した絞り込み軸を持ちます:

### 軸 A — 強制（`tool_allow` / `tool_deny`）

ツールレベルの許可/拒否制御。既存のパーミッションレイヤーと並んでライブ ∩ ゲートに乗る `ContextualPermission` を生成します。

| フィールド | 型 | セマンティクス |
|-----------|-----|--------------|
| `tool_allow` | `list[str] \| null` | 許可リスト。`null` = 制約なし（拒否リストのみ）。 |
| `tool_deny` | `list[str]` | 拒否リスト。合成プロファイル間で拒否の和集合。 |

同一ツール名に対しては拒否エントリが許可エントリより常に優先されます。

### 軸 B — 可視性（`categories`）

認知的絞り込み: エージェントに可視なままにするツールカテゴリ。正規 12 エントリのカタログ（`CATEGORIES`）に対して `categories` から導出されます。

| フィールド | 型 | セマンティクス |
|-----------|-----|--------------|
| `categories` | `list[str] \| null` | **可視のままにする**カテゴリ。`null` = 絞り込みなし（全て可視）。`[]` = 全て非表示。 |

不明なカテゴリ名は no-op（前方互換 — エラーではない）。

注意: `visible ⊆ authorized` は構造的に成立します — 可視性軸はツールを非表示にするだけで、強制軸が拒否したツールを再付与することはできません。

## 合成モデル

複数のプロファイルが同時に適用される場合、`compose_resolved` は**最も制限的なものが勝つ**ルールで統合します:

- `tool_deny` → **和集合**（いずれかのプロファイルの拒否が勝つ）
- `tool_allow` → 全ての制約ある許可リストの**共通集合**（`null` = ⊤、スキップ）; 全ての制約プロファイルが許可する場合のみツールが許可される
- `excluded_categories` → **和集合**（いずれかのプロファイルの非表示が勝つ）

空のプロファイルリスト → 不活性な結果（プロファイルなしと同一）。

## コンテキスト自動 untrusted 絞り込み（S4）

1 つのプロファイルは、明示的なバインディングなしに自動的に適用されます — エージェントのコンテキストにアンドラステッドな外部コンテンツがライブで存在する間:

**プロファイル名:** `_untrusted`（ビルトインのセキュアデフォルト、`.reyn/capability_profiles/_untrusted.yaml` でオーバーライド可能）

**トリガー:** メタに `external_source=true` を持つ任意の履歴/コンテキストエントリ（コンテンツフェンスのシームが取り込み時にスタンプ）。

**ビルトインデフォルトの拒否セット:** メモリ書き込み/削除、再委譲、サンドボックス実行、MCP インストール。目標: アンドラステッドなコンテンツは読み取りと推論が可能だが、不可逆アクションを駆動できない。

これはシーム非依存 — トリガーはメタマーカーであり、特定のソースではありません。

## バインディングモード

`CapabilityProfile` は 2 つのバインディングモードのいずれかで実行中のセッションに適用されます:

- **エージェントごとのデフォルト** — エージェントのデフォルトとして割り当てられた 1 つのプロファイル。
- **コンテキスト合成可能** — ライブコンテキストからのプロファイルの動的合成（例: アンドラステッドソース絞り込み、エフェメラルタスクスコープ）。

プロファイルがエージェント、トポロジー、エフェメラルスコープにバインドされる正確な inline-vs-ref メカニズムは確定中です（⏳ #2074-S4）。S4 がランドするまで、コンテキスト自動 untrusted バインディングのみがエンドツーエンドで配線されています。

## エージェントの自己編集

エージェントは追加のパーミッションを要求せずにケイパビリティプロファイルを作成・更新できます:

**パス:** `.reyn/capability_profiles/<name>.yaml`

**書き込みパーミッション:** `.reyn/capability_profiles/` はデフォルトの書き込みゾーン（`.reyn/`）内です。保護されたパスではありません（`.reyn/approvals.yaml` とは異なり）。標準的な `file.write` には**追加の宣言は不要**です。

**検証済み:** `_DEFAULT_WRITE_ZONES = (".reyn",)` であり、`_CANONICAL_PROTECTED_WRITE_PATHS` には `.reyn/approvals.yaml` と `.reyn/index/sources.yaml` のみが含まれます。`src/reyn/security/permissions/permissions.py` で確認済み。

**手順:** 目的の `categories` / `tool_allow` / `tool_deny` 軸を含む YAML ファイルを書き込みます。プロファイル名（ファイルステム）はバインディング時の参照名です（⏳ S4 配線）。

**例:**

```yaml
name: read-only-researcher
description: "全書き込み/実行サーフェスを拒否; 読み取りカテゴリのみ許可。"
categories:
  - file
  - web
tool_deny:
  - exec__sandboxed_exec
  - memory_operation__remember_shared
```

## スキーマ例

```yaml
# .reyn/capability_profiles/read-only-researcher.yaml
name: read-only-researcher        # 必須 (== ファイルステム)
description: ""                   # オプション、デフォルト ""
categories:                       # オプション; null = 全て可視
  - file
  - web
tool_allow: null                  # オプション; null = 制約なし (拒否リストのみ)
tool_deny:                        # オプション、デフォルト []
  - exec__sandboxed_exec
  - multi_agent__delegate
```

## ∩ モデルとの関係

`CapabilityProfile` の強制軸はランタイム ∩ 項として `ContextualPermission` を生成します — restrict-only であり、他のレイヤーが既に拒否したケイパビリティを昇格させることはできません。

`AgentProfile.allowed_skills` / `allowed_mcp` フィールドは同じ ∩ モデルの `ProfileLayer` として参加します。

完全な ∩ モデルについては、[パーミッションモデル § 論理積制限モデル](permission-model.md#effective-permission-conjunctive-restrict-model) を参照してください。

## 参照

- [パーミッションモデル](permission-model.md) — ∩ モデル、認可レイヤー
- [パーミッションモデル § 論理積制限](permission-model.md#effective-permission-conjunctive-restrict-model) — ∩ における ProfileLayer
- [リファレンス: profile.yaml](../../reference/dsl/profile-yaml.md) — AgentProfile スキーマ（allowed_skills、allowed_mcp）
- [コンセプト: マルチエージェント](../multi-agent/multi-agent.md) — エージェントの組み合わせ
- [リファレンス: reyn agent CLI](../../reference/cli/agent.md) — `reyn agent new`、`reyn agent list`
