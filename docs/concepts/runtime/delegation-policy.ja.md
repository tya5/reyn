---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [delegation policy, default-deny delegation, _delegate profile, capability_default, delegate floor, no laundering, recursive delegate, delegation-unsafe, gateway:delegation-unsafe, reyn audit, DelegationConfig, resolved_profile_for, is_delegate, FLOORED_DENY_CLASSES]
---

# 委任ポリシー

委任ポリシーは、エージェントが委任ターゲットとして生成されるときに受け取るケイパビリティサーフェスを制御します。デフォルトでは、委任されたエージェントはスポーナーの完全なケイパビリティを継承します — ポリシー前の動作と同じです。オプションで**デフォルト拒否**モードを使用すると、ペートポロジーバインディングを必要とせずに、すべてのアンバウンド委任エージェントを制限的なフロアに絞り込みます。

## 設定

```yaml
# reyn.yaml
delegation:
  capability_default: deny   # デフォルト: inherit
```

| 値 | 動作 |
|----|------|
| `inherit`（デフォルト） | 委任エージェントはスポーナーのケイパビリティサーフェスを継承します — ポリシー前と同一です。 |
| `deny` | アンバウンド委任エージェントは `_delegate` フロアを受け取ります（以下参照）。 |

影響を受けるのは**アンバウンド委任エージェントのフォールバック**のみです: トップレベルエージェントとトポロジーバインドされた委任エージェントは、この設定にかかわらず変更されません。

## `_delegate` フロア

`capability_default=deny` の場合、アンバウンド委任エージェント — A2A リクエストパス経由で生成され、トポロジー `capability_profile` バインディングを持たないもの — は組み込みの `_delegate` プロファイルで絞り込まれます。

**組み込み拒否セット**（`_untrusted` プロファイルと同じタクソノミー、`_FLOORED_DENY_CLASSES` から単一ソース化）:

| クラス | 拒否されるツール | 理由 |
|--------|----------------|------|
| `re-delegation` | `multi_agent__delegate`、`delegate_to_agent` | アンバウンド委任エージェントからの無制限スポーニングチェーンを防止 |
| `exec` | `exec__sandboxed_exec`、`sandboxed_exec` | 実行には明示的なオペレーター認証が必要 |
| `mcp-install` | `mcp__install_registry`、`mcp__install_package`、`mcp__install_local` | MCP サーバーインストールは高権限のオペレーター管理アクション |
| `memory-write` | `memory_operation__remember_shared`、`memory_operation__remember_agent`、`memory_operation__forget` | アンバウンド委任エージェントからの永続化には意図的なオプトインが必要 |

フロアはオーバーライド可能です: オペレーターファイル `.reyn/capability_profiles/_delegate.yaml` が組み込みプロファイルを置き換えます。不正なオーバーライドは組み込みにフォールバックします（stderr に出力）— タイプミスでフロアが暗黙的に削除されることはありません。

## バインディングがフロアを置き換える（= バインディングが再付与）

トポロジー `capability_profile` バインディングは `_delegate` フロアと**合成されるのではなく、置き換えます**。これが再付与メカニズムです:

- **アンバウンド委任エージェント** → `_delegate` フロアが適用されます。
- **バウンド委任エージェント** → トポロジーバインディングがフロアを置き換えます。バインドされたプロファイルがそのエージェントの完全な ContextualLayer となり、`_delegate` フロアは追加で合成されません。

理由: `compose_resolved` は最も制限的なものが勝つルールです。`exec` を拒否するフロアと `exec` を許可するプロファイルを合成すると、まだ拒否されてしまいます — フロアは付与不可能になります。代わりに、レジストリは委任エージェントがアンバウンドの場合にのみフロアを適用します; バインディングがある場合は、オペレーターがそのロールに対してケイパビリティを意図的に表明していることを意味します。

## 再帰的伝播（ローンダリング不可）

`is_delegate` フラグは、スポーナー自身のステータスに関係なく、**すべての A2A リクエストパスロード**で設定されます。トポロジーバインディングを受け取って `exec` が再付与された再付与済みコーディネーターも、自身のサブ委任エージェントに対して `is_delegate=True` を設定します。それらのサブ委任エージェントは、アンバウンドであれば、まだ `_delegate` フロアを受け取ります。

**結果**: 再付与済みコーディネーターは、自身のより広いケイパビリティを通じてアンバウンドサブ委任エージェントにフロアを「ローンダリング」することはできません。フロアは委任チェーンのすべてのホップに伝播します。

## `reyn audit` — `gateway:delegation-unsafe`

`reyn audit` コマンドは、プロジェクトのトポロジーとケイパビリティプロファイルを静的スキャンして危険なクラスの再付与を検出する委任安全ルール（`gateway:delegation-unsafe`、ルール 4）を含みます。

### スキャン対象

**OPT-A 到達可能性精密スコープ**: インバウンド `can_send` エッジを持つロール（= A2A リクエストパスの実際の委任ターゲット）のみがフラグ付けされます。アウトバウンドのみのロール（例: `delegate_to_agent` を正当に保持する階層のトップコーディネーター）はインバウンド委任パスを持たず、委任ターゲットではないため、フラグ付けされません — 誤った HIGH exit を回避します。

**`_delegate.yaml` オーバーライド**: オーバーライドファイルは無条件でスキャンされます（到達可能性チェック不要 — これはグローバルなアンバウンド委任エージェントフロアです）。

### フィンディング

| フィンディング | 重大度 | 条件 |
|--------------|--------|------|
| バウンドプロファイルがクラスを再付与 | HIGH | `re-delegation` または `exec` クラスが許可 |
| バウンドプロファイルがクラスを再付与 | MED | `memory-write` または `destructive-fs` クラスが許可 |
| `_delegate.yaml` がクラスを再付与 | HIGH / MED | 同クラス対重大度マッピング |
| ポスチャーナッジ | INFO | `capability_default=inherit` で任意のトポロジーに委任エッジがある |

`destructive-fs` クラス（`delete_file`、`file__delete`）は**監査のみ** — FILE_WRITE パーミッションシステムでゲートされているためランタイム `_delegate` フロアには含まれていませんが、委任可能ロールへの再付与判断として監査で表面化されます。

**終了動作**: `reyn audit` は HIGH フィンディングの場合にのみ非ゼロで終了します — CI セーフです（HIGH はデプロイをブロック; MED と INFO は情報提供のみ）。

### 監査クラス

| クラス | 重大度 | ツール |
|--------|--------|--------|
| `re-delegation` | HIGH | `multi_agent__delegate`、`delegate_to_agent` |
| `exec` | HIGH | `exec__sandboxed_exec`、`sandboxed_exec` |
| `mcp-install` | HIGH | `mcp__install_registry`、`mcp__install_package`、`mcp__install_local` |
| `memory-write` | MED | `memory_operation__remember_shared`、`memory_operation__remember_agent`、`memory_operation__forget` |
| `destructive-fs` | MED | `delete_file`、`file__delete`（監査のみ、ランタイムフロア外） |

### 使用法

```
reyn audit                     # 全スキャン
reyn audit --json              # CI パイプライン向け JSON 出力
```

## エンドツーエンドフロー

```
A → B に委任
      ↓
   is_delegate=True
      ↓
   registry.resolved_profile_for("B", is_delegate=True)
      ↓
   トポロジーバインディングなし?
     ├── capability_default=inherit → (None, frozenset())  [ポリシー前]
     └── capability_default=deny   → _delegate フロア適用

B → C に委任
      ↓
   is_delegate=True  (常時 — 再帰的)
      ↓
   C アンバウンド → _delegate フロア  (B がバインディングで再付与されていても)
```

## 参照

- [コンセプト: ケイパビリティプロファイル § デフォルト拒否委任絞り込み](capability-profile.md#default-deny-delegation-narrowing-2081) — ケイパビリティプロファイル概要の `_delegate` セクション
- [コンセプト: ケイパビリティプロファイル](capability-profile.md) — 完全な ∩ モデル、ProfileLayer vs ContextualLayer、自己編集
- [コンセプト: マルチエージェント](../multi-agent/multi-agent.md) — トポロジー、委任、`can_send` エッジ
- [リファレンス: reyn.yaml § delegation](../../reference/config/reyn-yaml.md) — `delegation.capability_default` 設定
