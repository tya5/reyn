# ADR-0027a: AuditSeal のハッシュチェーントポロジー

**Status**: Proposed
**Date**: 2026-05-13
**Depends on**: ADR-0027 (AuditSeal 分離)

---

## Context

ADR-0027 は `AuditSeal` を独立した compliance レイヤーとして定義し、各
スキル実行に `chain_hash` + `prev_seal` フィールドでハッシュチェーンを付与
する設計を示した。親 ADR では **チェーンの構成単位** — 新しい seal が指す
`prev_seal` は何か、並行または入れ子になったエージェントのチェーンがどう
合成されるか — という問いを defer した。

この defer は、マルチエージェント並行性モデル (ADR-0023 amendment §2.1 で
記述されるマルチプロセス計画) と audit verifier の実装戦略が ADR-0027
執筆時点で確定していなかったためである。

チェーントポロジーの選択は以下に直接影響する:

- `reyn audit verify` コマンドが seal チェーンを走査する方法
- 並行実行中の 2 エージェントが単一の検証可能なチェーンを生成するか、
  独立した検証可能なチェーンを生成するか
- plan-mode (1 つの plan が複数の skill run を spawn) の表現方法
  (sub-ADR 0027c で詳細を扱う)

---

## Decision drivers

- **マルチエージェント並行実行**: Reyn は `delegate_to_agent` と
  `plan`-mode による複数の並行 skill run をサポートし、複数エージェントに
  またがる場合がある。
- **マルチプロセス計画**: ADR-0023 Phase 2.1 では plan ツールが step ごとに
  async task を生成することが確立されており、将来のマルチプロセス拡張では
  これがさらに進む。
- **Audit verifier 実装コスト**: verifier は決定論的かつ再現可能な方法で
  チェーンを走査できなければならない。
- **Plan-mode 統合**: seal_unit は plan 境界と相互作用する (専用分析は
  sub-ADR 0027c を参照)。
- **エンタープライズ compliance 要件**: 規制環境では監査対象単位
  (ワークフロー / エージェント / 実行) ごとに途切れのないチェーンが期待される。
- **OSS ライトユーザーの利便性**: シングルプロセスユーザーが forest
  トポロジーを管理する必要がないこと。

---

## 検討した Options

### Option A: エージェント単位の時系列 single chain

各エージェントが独自の時系列チェーンを維持する。新しい skill run の seal は
**同一エージェント** が直前に生成した seal を `prev_seal` として参照する。

```
agent-alice:  [seal-1] → [seal-2] → [seal-3]
agent-bob:    [seal-1] → [seal-2]
```

**Pros:**
- シンプル: 各エージェントの seal ディレクトリは独立したリンクリスト。
- クロスプロセスの順序付け調整が不要。
- Verifier はエージェントごとに単一の flat chain を走査すればよい。

**Cons:**
- クロスエージェント呼び出し (`delegate_to_agent` など) は構造的な接合が
  なく、親子関係は `run_id` メタデータのみで表現される。
- マルチエージェントワークフロー全体の integrity 検証には、チェーン間
  参照を追える verifier が必要。
- 「このチェーンはあのチェーンの子」という自然な表現がない。

### Option B: グローバル single chain (全エージェント共有)

全エージェントが 1 つの順序付きチェーンに書き込む。`prev_seal` は常に
どのエージェントが生成したかにかかわらずグローバルに最も新しい seal を指す。

```
global: [alice/seal-1] → [bob/seal-1] → [alice/seal-2] → [bob/seal-2]
```

**Pros:**
- チェーンが 1 つ; verifier の走査経路が 1 本。
- 全エージェントにわたる完全な活動順序。

**Cons:**
- マルチプロセス書き込みには分散ロックまたはシリアライゼーションポイントが
  必要で、ボトルネックと調整失敗モードを生む。
- 並行 skill 完了時の競合条件でチェーン順序が非決定論的になり、ビット完全な
  検証が困難。
- Reyn の将来的なマルチプロセス拡張と根本的に相容れない。

### Option C: ワークフロー単位の tree (seal tree の forest)

各トップレベルユーザーリクエスト (ワークフロー) がそれ自身の seal tree の
ルートとなる。子 skill run (plan や delegate が spawn したもの) はサブツリーを
形成し、各 seal が親 seal を参照する。

```
workflow-w1:
  root (plan seal)
  ├── [step-1/seal]
  ├── [step-2/seal]
  └── [step-3/seal]

workflow-w2:
  root (plan seal)
  └── [step-1/seal]
```

**Pros:**
- plan-mode の自然な構造表現: plan seal がルート、子 skill run seal が参照。
- ワークフロー全体の integrity チェックがツリー走査で完結。
- グローバルな順序付け調整が不要。

**Cons:**
- plan レベルの seal が必要 (sub-ADR 0027c で検討); plan をメタデータ集約
  のみとする場合 (sub-ADR 0027c の Option C) は成立しない。
- Forest 管理: 各ワークフロールートを追跡する必要があり、クラッシュした
  plan の孤立ルートにはギャップ処理ポリシーが必要。
- Flat per-agent chain より verifier 実装が複雑。

### Option D: Hybrid — エージェント単位 chain + クロスエージェント参照リンク

各エージェントが独自のチェーンを維持 (Option A と同じ) しつつ、seal が
オプションの `parent_seal_ref` フィールドを持ち、委譲発生時に呼び出し元
エージェントのチェーン先頭 seal を指す。

```
agent-alice:  [seal-1] → [seal-2 (plan spawned)]
                                ↓ parent_ref
agent-bob:    [seal-1 parent=alice/seal-2] → [seal-2]
```

**Pros:**
- シングルエージェントケースでは Option A のシンプルさを保つ。
- クロスエージェント関係が明示的かつ機械的に走査可能。
- グローバル調整不要; エージェント単位チェーンは独立。

**Cons:**
- Verifier が flat chain walk とクロスチェーン参照解決の両方を実装する必要。
- `parent_seal_ref` はデリゲーション時に設定が必要だが、プロセス境界を
  またぐ委譲の場合、子 seal 書き込み後に参照が届く可能性がある。

---

## Recommendation (proposed direction)

**Option D (hybrid)** を推奨する。クロスエージェント参照管理が実装時に
複雑すぎることが判明した場合は **Option A をフォールバック** とする。

理由:
- Option A は現在の一般的なシングルエージェント / シングルプロセス環境で
  の正しいベースライン。
- Option D は Option A を壊さずに拡張する: `parent_seal_ref` フィールドは
  オプションであり、無視する verifier は Option A の動作に gracefully degrade。
- Option B はマルチプロセス調整要件のため除外。
- Option C は plan レベルの seal を前提とし、sub-ADR 0027c の未解決問いに
  依存する。Option D はその解決を必要としない。

**実装着手時の再判断**: sub-ADR 0027c が「plan が独自の seal を持つ」
(その sub-ADR の Option B または D) に解決した場合は Option C を再検討する —
ワークフローレベルの監査完全性において Option C が望ましい可能性がある。

本 recommendation は実装着手時に再判断すること。

---

## Open questions

1. 委譲がプロセス境界をまたぐ場合、`parent_seal_ref` は同期的 (デリゲーション
   開始をブロック) に設定するか、非同期的 (子 seal に対する後付け修正として)
   に設定するか?
2. Option D において、単一エージェントのチェーン内で並行 skill run が順不同で
   完了する場合の seal 順序は完了時刻順か開始時刻順か?
3. 委譲先エージェントが seal を生成する前にクラッシュした場合、親エージェントの
   チェーンにはギャップエントリーが入るか、それとも AuditContext レコードで
   のみ検出可能か?

---

## Related

- ADR-0027: AuditSeal 分離 (親 ADR)
- ADR-0027b: config_hash スコープ
- ADR-0027c: seal_unit と plan-mode 統合 (トポロジー選択と plan seal の有無が
  直接連動)
- ADR-0027d: writer 失敗時のセマンティクス
- ADR-0023: Plan-Mode Forward Replay (マルチエージェント async dispatch の文脈)
