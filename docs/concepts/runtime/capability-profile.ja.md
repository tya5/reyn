---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [capability profile, agent profile, allowed_skills, mcp filter, tool restriction, category visibility, profile.yaml, self-edit, hot-reload, autonomous edit]
---

# ケイパビリティプロファイル

ケイパビリティプロファイルは、エージェントごとの仕様で、2 つのことを宣言します: エージェントの**アイデンティティ**（name、role）と**ケイパビリティ制限**（使用できるスキル・MCP サーバー・ツール・ツールカテゴリ）。

`.reyn/agents/<name>/profile.yaml` に格納され、セッション構築時に読み込まれます。`default` エージェントは `reyn chat` の初回実行時に自動的に作成されます。

## 1 つの仕様、2 つのバインディングアダプター

ケイパビリティプロファイルは 1 つのデータで、2 つの異なるコンシューマーがあります:

```
profile.yaml
    │
    ├─→ AgentLayer   (認可付与ベースライン: スキル許可リスト → ルーターカタログ)
    │
    └─→ ProfileLayer (論理積制限: AgentLayer + SandboxLayer との ∩)
```

**AgentLayer** はプロファイルを使って、ルーター LLM に見せる前にスキルカタログをフィルタリングします。「このエージェントに何ができるか」というサーフェス — ルーターが選択できるオプションです。

**ProfileLayer** はランタイムの論理積制限モデル（`effective = AgentLayer ∩ SandboxLayer ∩ ProfileLayer`）に参加します。ProfileLayer は制限専用です: AgentLayer が付与したものを絞り込むことはできますが、AgentLayer が拒否したものを再付与することはできません。論理積は構造的 — どのレイヤーの `False` もオーバーライドできません。

両方のアダプターが同じプロファイル仕様を読み込みます。プロファイルに制限を追加すると、`EffectivePermission` のロジックを変更せずに、カタログ（AgentLayer）とランタイムゲート（ProfileLayer）の両方が同時に絞り込まれます。

完全な ∩ モデルについては、[パーミッションモデル § 論理積制限モデル](permission-model.md#effective-permission-conjunctive-restrict-model) を参照してください。

## ケイパビリティ軸

統合プロファイル仕様は 4 つの制限軸を持ちます。すべてオプションです; 省略または `null` の軸はその次元で無制限を意味します。

### `allowed_skills`（スキル許可リスト）

ルーター LLM に提示するスキルを制御します。

| 値 | 意味 |
|----|------|
| 省略 / `null` | **無制限。** プロジェクト + stdlib の全スキルを提示します。 |
| `[]` | **ルーターのみ。** スキルのスポーンなし; ルーターは直接返答するか委譲できます。 |
| `[a, b, c]` | **許可リスト。** 列挙したスキル名のみを提示します。 |

システムスキル（`skill_router`、`chat_compactor`、`skill_narrator`）は常に有効 — このリストの対象外です。

2 層の強制: ルーターは LLM がカタログを見る前に `available_skills` を絞り込み; `_spawn_skill` は多層防御としてスポーン時に再チェックします。

### `allowed_mcp`（MCP サーバー許可リスト）— ⏳ staged: #2074-S1

インストール済みのサーバーとは独立に、エージェントが呼び出せる MCP サーバーを制限します。MCP 呼び出し時にエージェントごとの論理積でフィルタリングします。

`null` = 無制限（設定されたすべてのサーバーが利用可能）。リストは指定したサーバー ID に制限します。

注意: `allowed_mcp` は ACL フィルターであり、ケイパビリティ付与ではありません — すでに付与された `mcp` パーミッションを絞り込むものであり、単独で MCP アクセスを付与するものではありません。[パーミッションモデル § allowed_mcp](permission-model.md#axes) を参照してください。

### `tool_policy`（ツールごとの許可/拒否）— ⏳ staged: #2074-S1

ツールが LLM に届く前にディスパッチ時に適用される、ツール名ごとの許可または拒否エントリ。

`null` = 無制限。`{tool: <name>, policy: allow|deny}` エントリのリスト。同一ツール名に対しては拒否エントリが許可エントリより優先されます。

### `category_visibility`（ツールカテゴリの可視性）— ⏳ staged: #2074-S1

エージェントに可視なツールカテゴリを制御します。カテゴリはツールを機能でグループ化します（例: `file`、`shell`、`web`、`mcp`）。

`null` = すべてのカテゴリが可視。リストは指定したカテゴリのみに可視性を制限します。

## リロードモデル

プロファイルの変更は**次のセッション起動時**に有効になります。プロファイルはセッション構築時に 1 回読み込まれます; 実行中のセッションはメモリ内コピーから読み込みます。

**ターン境界でのホットリロードが計画されています** — `profile.yaml` の編集が、セッションを再起動せずにターン間で反映されるようになります。これは自律編集ワークフローの一部として設計中です（⏳ #20、#2074 の後にシーケンス）。

## エージェントの自己編集

エージェントは追加のパーミッションを要求せずに、ランタイムに自分自身のケイパビリティプロファイルを編集できます:

**パス:** `.reyn/agents/<agent_name>/profile.yaml`

**書き込みパーミッション:** `.reyn/` ツリーはデフォルトの書き込みゾーンです。`.reyn/agents/` は保護されたパスではありません（`.reyn/approvals.yaml` とは異なり）。このパスへの標準的な `file.write` には**追加の宣言は不要** — デフォルトの書き込み付与の範囲内です。

**検証済み:** `_DEFAULT_WRITE_ZONES = (".reyn",)` であり、`_CANONICAL_PROTECTED_WRITE_PATHS` には `.reyn/agents/` は含まれません（`src/reyn/security/permissions/permissions.py` で確認済み）。

**手順:** 現在の `profile.yaml` を読み込む → 該当の軸を変更 → 書き戻す。変更は次の起動時（現在）または次のターン時（⏳ ホットリロード、#20）に有効になります。

**自己編集のユースケース:** 典型的な自律編集の目的は、エージェントがセッション中に自分自身のケイパビリティプロファイルを絞り込むことです — 例えば `allowed_skills: [skill_a, skill_b]` と書いて集中したタスクセットに自分を制限します。ホットリロード（#20）が実装されると、次のターン境界で即座に有効になります。

## スキーマ例

```yaml
name: researcher
role: |
  深い技術調査。一次ソースを優先。
created_at: 2026-05-01T12:00:00+00:00
allowed_skills:
  - web_search
  - recall_docs
# 以下の軸は #2074-S1 で staged:
allowed_mcp:          # null = 無制限
  - github-mcp
tool_policy:          # null = 無制限
  - tool: shell_exec
    policy: deny
category_visibility:  # null = すべて可視
  - file
  - web
```

フルスキーマリファレンス: [profile.yaml リファレンス](../../reference/dsl/profile-yaml.md)。

## 参照

- [パーミッションモデル](permission-model.md) — ∩ モデル、認可レイヤー、軸の分類
- [パーミッションモデル § 論理積制限](permission-model.md#effective-permission-conjunctive-restrict-model) — ∩ における ProfileLayer
- [リファレンス: profile.yaml](../../reference/dsl/profile-yaml.md) — フルスキーマ + エージェント自己編集ガイド
- [リファレンス: reyn agent CLI](../../reference/cli/agent.md) — `reyn agent new`、`reyn agent list`
- [コンセプト: マルチエージェント](../multi-agent/multi-agent.md) — エージェントの組み合わせ方
