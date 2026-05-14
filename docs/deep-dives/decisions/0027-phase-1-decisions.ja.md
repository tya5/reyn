# ADR-0027 Phase 1 着手前 ユーザー判断事項 (User Judgment Gates)

**Parent**: ADR-0027 AuditSeal 分離
**Related**: ADR-0027a / ADR-0027b / ADR-0027c / ADR-0027d
**Status**: Pending User Confirmation
**Created**: 2026-05-14

---

## Context

ADR-0027 AuditSeal の Phase 1 (= 単一エージェント内ハッシュチェーン seal) を
実装する前に、5 つの判断についてユーザーの確認が必要。各 sub-ADR (0027a–d) は
推奨 option を提示しているが、最終決定はユーザーに委ねられている。
本 doc は 5 gate を 1 箇所に集約し、1 回のパスで確認を得るための
work artifact。

各 gate の構成:
- 判断の問い
- 検討した options (pros/cons は対応する sub-ADR から転記)
- recommendation とその根拠
- ユーザー最終決定用チェックボックス

Phase 1a の実装着手は **5 gate すべての確認後**。

---

## Gate 1: ハッシュチェーントポロジー デフォルト (ADR-0027a)

**判断の問い**: Phase 1 実装において、AuditSeal はエージェント単位の
チェーンのみ (Option A) を使うか、クロスエージェント参照リンク付きの
ハイブリッド (Option D) を使うか。

### Options

**Option A — エージェント単位の時系列 single chain** *(Phase 1a 推奨)*

各エージェントが独自の時系列チェーンを維持。新 skill run の seal は
同一エージェントが直前に生成した seal を `prev_seal` として参照する。

| | |
|---|---|
| Pros | Flat-list verifier がシンプル; クロスプロセス調整不要; シングルエージェントユースケースを完全カバー |
| Cons | クロスエージェント呼び出しが構造的な接合なしに別々のチェーンを生成; マルチエージェント compliance ワークフローで親子委譲を構造的に追跡できない |

**Option D — ハイブリッド: エージェント単位チェーン + オプション `parent_seal_ref`**

Option A と同様だが、委譲発生時に呼び出し元エージェントのチェーン先頭
seal を指す `parent_seal_ref` フィールド (オプション) を seal に追加する。

| | |
|---|---|
| Pros | シングルエージェント実行では Option A のシンプルさを保つ; クロスエージェント関係が機械的に走査可能; グローバル調整不要 |
| Cons | Verifier が flat-chain walk とクロスチェーン参照解決の両方を実装する必要 (+1-2 日); プロセス境界をまたぐ `parent_seal_ref` の設定には慎重な順序付けが必要 |

**Option B** (グローバル single chain) は除外 — マルチプロセス書き込み調整が
Reyn の将来的なマルチプロセス拡張と相容れない。

**Option C** (ワークフロー単位 tree) は plan レベルの seal に依存 (Gate 3);
Gate 3 が Option B に解決した後にのみ実行可能。

### Recommendation

Phase 1a で **Option A** を実装。Phase 1c (plan-mode と verifier 実装完了後)
で **Option D** にアップグレード。`parent_seal_ref` フィールドはオプション拡張と
してスキーマに後付けでき、Phase 1a の seal を破壊しない。

### ユーザー決定

- [ ] Option A (Phase 1a ベースライン、Phase 1c で D にアップグレード) — *推奨*
- [ ] Option D (クロスエージェント参照を最初から実装)
- [ ] その他 (記述):

---

## Gate 2: config_hash スコープ (ADR-0027b)

**判断の問い**: `AuditContext` の `config_hash` フィールドには何をハッシュするか —
スキル定義のみ (Option B)、モデル設定のみ (Option C)、それとも複数の独立した
サブハッシュの階層構造 (Option D) にするか。

### Options

**Option A — reyn.yaml 全体のハッシュ**

| | |
|---|---|
| Pros | ファイル 1 つでシンプル; すべての設定変更を捕捉 |
| Cons | `logging.level` など無関係なセクションの変更でも seal が無効化されてノイズが多い |

**Option B — スキル定義ハッシュのみ**

スキルの `skill.md` と参照するすべてのフェーズファイルをハッシュする。

| | |
|---|---|
| Pros | 「このスキルの定義は変更されたか?」に直接答える; 無関係な reyn.yaml 変更に安定; スキル粒度 |
| Cons | モデルプロバイダー切替や OS レベルの設定変更を検出しない |

**Option C — モデル設定ハッシュのみ**

実効プロバイダー、モデル名、推論パラメーターをハッシュする。

| | |
|---|---|
| Pros | 「宣言された LLM が使われたか?」に直接答える; Hermes #487 の再現性ポジショニングに沿う |
| Cons | スキル定義の変更を検出しない; モデルバージョンエイリアスが不安定 |

**Option D — 階層化: 複数の独立したサブハッシュフィールド** *(推奨)*

```json
{
  "config_hash": {
    "skill_def": "sha256:...",
    "model_cfg": "sha256:...",
    "os_cfg":    "sha256:..."
  }
}
```

Verifier はコンプライアンス要件に応じていずれかまたはすべてのサブハッシュを
独立して確認できる。古い verifier は未知のフィールドを無視する (前方互換)。

| | |
|---|---|
| Pros | 最大の柔軟性; 無関係な変更によるノイズなし; compliance 監査可能性と再現性の両目標に対応 |
| Cons | 3 つのハッシュ入力を定義・計算・維持する必要; `os_cfg` スコープはサブ決定が必要; verifier エラーレポートでどのサブハッシュが不一致か特定する必要 |

Option D の最小初期スコープ:

| サブハッシュ | ハッシュ対象 |
|---|---|
| `skill_def` | `skill.md` + 参照するすべてのフェーズファイル (正規化コンテンツ) |
| `model_cfg` | 実効プロバイダー + モデル名 + 主要推論パラメーター (temperature、max_tokens) |
| `os_cfg` | reyn.yaml の `audit.*` セクション (初期は狭いスコープ; 需要に応じて拡張) |

階層化アプローチが最初のリリースに複雑すぎる場合は、**Option B** にフォールバックし、
`model_cfg` ハッシュをフォローアップで追加するという設計メモを残す。

### Recommendation

完全カバレッジには **Option D (階層化)**。Phase 1a の実装コストが高い場合は
**Option B を最小限の有効フォールバック** として使用。

### ユーザー決定

- [ ] Option D (階層化、完全カバレッジ) — *推奨*
- [ ] Option B (スキル定義ハッシュのみ、最小限フォールバック)
- [ ] その他 (記述):

---

## Gate 3: Plan-mode seal 境界 (ADR-0027c)

**判断の問い**: プラン実行 (複数の並行 skill run を spawn) は独自の
`PlanSeal` アーティファクトを生成すべきか、それとも plan レベルの監査
カバレッジは `plan_id` で子スキル実行 seal を照会することで達成すべきか。

### Options

**Option A — plan は seal 単位でない; skill run のみ** *(Phase 1a 推奨)*

skill run のみが sealed になる。プランレベルの監査は `plan_id` を共有する
すべての seal を照会することで再構成する。

| | |
|---|---|
| Pros | `seal_unit: skill` ベースラインに変更なし; `PlanRuntime` が AuditSeal ライフサイクルフックを必要としない; 最もシンプルな実装パス |
| Cons | 「このプランが最後まで実行された」ことを証明する単一アーティファクトがない; 部分実行の検出に複数 seal の結合が必要; `plan_step_completed` WAL イベントに対応する seal 境界がない |

**Option B — プランが独自の `PlanSeal` を持ち、子 seal がそれを参照する**

`PlanSeal` はプラン完了時 (またはクラッシュ時) に `step_count_expected` /
`step_count_completed` フィールドと共に生成され、子スキル実行 seal が
`plan_seal_ref` を持つ。

| | |
|---|---|
| Pros | プラン実行全体の単一監査アーティファクト; ステップ数の不一致で部分完了が即座に可視化; Gate 1 の Option C トポロジーを可能にする |
| Cons | `PlanRuntime` に AuditSeal フックの追加が必要; `PlanSeal` は新しいアーティファクト型; クラッシュ時の部分 seal 動作を Gate 4 で解決する必要がある |

**Option C — skill run 独立; verifier が `plan_id` で集約**

Option A と同様だが、マニフェストファイルを別途保存 (ハッシュチェーン外)。
integrity 検証を verifier に委ねる。

| | |
|---|---|
| Pros | `AuditSeal` スキーマがシンプルなまま |
| Cons | マニフェスト integrity が暗号的に証明されない; verifier が完全性チェックのためにプラン構造を知る必要がある |

**Option D — 委譲エージェントがプランを独自チェーンで管理**

プランコーディネーターを委譲エージェントのチェーン内の「skill run」として
扱う; 子 run が `parent_seal_ref` で参照 (Gate 1 Option D に連動)。

| | |
|---|---|
| Pros | 新しいアーティファクト型なしでエージェント単位トポロジーを再利用 |
| Cons | プラン「seal」とスキル実行 seal の区別にスキーマ拡張が必要; Gate 1 の決定に結合する |

### Recommendation

Phase 1a では **Option A** (plan seal なし; 子 seal に `plan_id` メタデータ)。
**Option B** はマルチステップワークフロー向け `reyn audit verify` を実装する
フォローアップ PR で追加。Option B は Gate 4 の解決 (クラッシュ時の `PlanSeal`
writer 失敗セマンティクス) に依存。

### ユーザー決定

- [ ] Option A (Phase 1a ベースライン、フォローアップで Option B 追加) — *推奨*
- [ ] Option B (最初から PlanSeal を実装)
- [ ] その他 (記述):

---

## Gate 4: Writer 失敗時のデフォルト (ADR-0027d)

**判断の問い**: AuditContext 書き込み (skill 開始時) または AuditSeal 書き込み
(skill 完了時) が失敗した場合、OS は何をすべきか — fail-open (レコードなしで続行)、
fail-closed (ブロック/アボート)、それともオペレーターが環境に応じて選択する
設定可能なモード (Option D) にするか。

### Options

**Option A — fail-open (skill は継続; 欠落レコードは事後検出)**

書き込み失敗はログに記録され、skill は通常通り続行する。

| | |
|---|---|
| Pros | 監査インフラ障害が skill の機能に影響しない; 長時間実行の skill がアボートされない |
| Cons | compliance のギャップが監査スウィープまで検出されない; context 書き込みがサイレントに失敗した場合、最終的な seal に `run_id` アンカーがない |

**Option B — fail-closed (context 書き込みが skill 開始をブロック; seal 書き込みはリトライ後イベント発行)**

context 書き込み失敗が skill 開始を阻止する。seal 書き込みは N 回リトライ;
消耗後に `seal_write_failed` を発行し、不完全な監査証跡として扱う。

| | |
|---|---|
| Pros | 強い compliance 保証; context 書き込み時のブロックは低コスト (何も計算されていない); 明示的なリトライ自体が監査可能 |
| Cons | 一時的なディスク障害でユーザーリクエストが作業開始前にアボートされる; 長時間実行後の seal 書き込み失敗では実質的にアボートできない (出力はすでに存在する) |

**Option C — degraded モード (メモリ内保持 + バックグラウンドリトライ)**

失敗時にメモリに保持し、設定可能なウィンドウ内でバックグラウンドタスクが
リトライする。ウィンドウが期限切れになると `seal_degraded` を発行して
fail-open にフォールバック。

| | |
|---|---|
| Pros | 一時的な I/O エラーを skill を失敗させずに処理; `seal_degraded` が verifier で検出可能なシグナルを生成 |
| Cons | メモリ内保持はプロセスクラッシュで失われる — これは AuditSeal が監査可能にすべきまさにそのシナリオ; バックグラウンドリトライが OS の複雑度を増加 |

**Option D — reyn.yaml で設定可能** *(推奨)*

```yaml
audit:
  writer_failure:
    context: fail-open   # または: fail-closed
    seal:    fail-open   # または: fail-closed、degraded
    retry_count: 3
```

オペレーターが環境に応じたモードを選択。デフォルトは両方 fail-open。
エンタープライズオペレーターが fail-closed にオプトイン。

| | |
|---|---|
| Pros | OSS とエンタープライズ両方の要件に対応; デフォルト動作に驚きがない |
| Cons | 設定表面積が増加; アクティブなモードに関わらず 3 モードすべてを実装する必要 |

### Recommendation

**Option D (設定可能)** + fail-open デフォルト。Phase 1a の最小限実装:
**fail-open のみ** を実装し、失敗時に `seal_write_failed` イベントを発行。
設定可能な fail-closed パスはエンタープライズ compliance 認定を対象とした
フォローアップ PR で追加。

### ユーザー決定

- [ ] Option D (設定可能; fail-open デフォルト; fail-closed は Phase 2 エンタープライズ追加) — *推奨*
- [ ] Option A (fail-open のみ、恒久的)
- [ ] その他 (記述):

---

## Gate 5: AuditContext スキーマスコープ

**判断の問い**: `AuditContext` の初期スキーマはどの程度の広さにするか —
compliance に必須の最小限フィールドセット (Option A)、それともランタイム
可観測性フィールドも含む拡張セット (Option B)。

### Options

**Option A — 狭い (compliance 必須のみ)** *(Phase 1a 推奨)*

```json
{
  "run_id":           "abc123",
  "skill":            "researcher",
  "invoked_by":       "user@example.com",
  "original_request": "...",
  "model":            "gemini-2.5-flash-lite",
  "model_version":    "...",
  "config_hash":      { ... },
  "started_at":       "2026-05-14T..."
}
```

verifier が「誰が、何を、どのモデルで、どの設定で、いつ実行したか」に
答えるために必要な最小限フィールド。

| | |
|---|---|
| Pros | スキーマが小さく拡張しやすい; 実装前にセマンティクスが不明確なフィールドへのコミットを避けられる |
| Cons | 可観測性ユースケース (遅延帰属、開始時トークンバジェットなど) には Events の別途照会が必要 |

**Option B — 拡張 (compliance + ランタイム可観測性)**

`token_budget_at_start`、`plan_id` (プランステップとして呼び出された場合)、
`workspace_path`、`agent_id`、`invocation_path` (入れ子 `run_skill` のスキル
コールスタック) などのフィールドを追加。

| | |
|---|---|
| Pros | compliance とデバッグの両クエリを単一レコードで回答; `plan_id` を context レコードに含めると PlanSeal なしでプランレベルのグルーピングが可能 (Gate 3 Option A をサポート) |
| Cons | スキーマが広くなり writer 実装が複雑化; `token_budget_at_start` などは OS ライフサイクルの順序によって context 書き込み時に利用できない場合がある |

### Recommendation

Phase 1a では **Option A (狭い)**。Phase 1b では最初の拡張として `plan_id`
(および `agent_id`) を追加 — Gate 3 Option A の verifier クエリに `plan_id`
が必要なため。`token_budget_at_start` と `invocation_path` は Phase 2 以降
に defer。

### ユーザー決定

- [ ] Option A (狭い; Phase 1b で `plan_id` 追加) — *推奨*
- [ ] Option B (最初から拡張スキーマ)
- [ ] その他 (記述):

---

## Summary

| Gate | トピック | Recommendation | 決定 |
|---|---|---|---|
| 1 | ハッシュチェーントポロジー | Option A → Phase 1c で D にアップグレード | [ ] |
| 2 | config_hash スコープ | Option D (階層化); Option B をフォールバックとして | [ ] |
| 3 | Plan-mode seal 境界 | Option A → Phase 1b/1c で B 追加 | [ ] |
| 4 | Writer 失敗時デフォルト | Option D (設定可能); Phase 1a は fail-open のみ | [ ] |
| 5 | AuditContext スキーマスコープ | Option A (狭い); Phase 1b で `plan_id` 追加 | [ ] |

Phase 1a の実装着手は 5 gate すべての確認後。

---

## Phase 1 実装シーケンス (参考)

Phase 1 は 6 週間のシーケンスとして提案されている:

| フェーズ | スコープ | 消費する Gate |
|---|---|---|
| **Phase 1a** (1-2 週目) | AuditContext writer + AuditSeal generator + ハッシュチェーン (Option A トポロジー) + `reyn.yaml` オプトイン + fail-open writer | Gates 1、4、5 |
| **Phase 1b** (3-4 週目) | 単一エージェントチェーン向け `reyn audit verify` CLI + AuditContext に `plan_id` 追加 + config_hash 実装 (Gate 2 選択) | Gates 2、5 拡張 |
| **Phase 1c** (5-6 週目) | クロスエージェント `parent_seal_ref` (Gate 1 → D の場合) + PlanSeal (Gate 3 → B の場合) + 設定可能 writer 失敗 (Gate 4 エンタープライズパスの場合) | Gates 1、3、4 拡張 |

---

## Related

- [ADR-0027: AuditSeal 分離](0027-audit-seal-separation.md)
- [ADR-0027a: ハッシュチェーントポロジー](0027a-audit-seal-hash-chain-topology.ja.md)
- [ADR-0027b: config_hash スコープ](0027b-audit-seal-config-hash-scope.ja.md)
- [ADR-0027c: Plan-mode 統合](0027c-audit-seal-plan-mode-integration.ja.md)
- [ADR-0027d: Writer 失敗セマンティクス](0027d-audit-seal-writer-failure-semantics.ja.md)
