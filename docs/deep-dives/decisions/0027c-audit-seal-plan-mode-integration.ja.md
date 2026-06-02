# ADR-0027c: AuditSeal の seal_unit と plan-mode 統合

**Status**: Proposed
**Date**: 2026-05-13
**Depends on**: ADR-0027 (AuditSeal 分離)、ADR-0023 (Plan-Mode Forward Replay)

---

## Context

ADR-0027 はデフォルトとして `seal_unit: skill` を定義しており、skill run の
完了ごとに 1 つの `AuditSeal` が生成される。設定では `seal_unit: phase` を
将来の拡張として許容している。

ADR-0023 (Plan-Mode Forward Replay) は、1 回のプラン実行 (`plan` ルーターツールで
起動) が `PlanRuntime` を通じて **複数の並行 skill run を spawn** できることを
確立している。プラングラフの各ステップは独立した `skill_run_id` にマップされる。
Phase 2.1 では、これらの skill run は独立した async タスクとして実行される。

これは構造的な問いを生む: 各 skill run が独自の `AuditSeal` を生成するなら、
監査レベルでプラン自体を何が表現するのか? プランは skill run ではなく、
skill run をディスパッチするコーディネーターだ。しかしプランは独自の `plan_id`
を持ち、`plan_step_completed` WAL イベントを生成し (ADR-0023 §3.2)、監査証跡
において明確な因果的位置を占める。

`plan_step_completed` イベントは `docs/concepts/runtime/events.md` で WAL 対象イベントとして
定義されている。それが seal 境界でもあるべきかがこの sub-ADR に defer された問いだ。

この sub-ADR は sub-ADR 0027a (ハッシュチェーントポロジー) と密接に連動している:
- 0027a で Option C (ワークフロー単位の tree) が選ばれた場合、プランがツリーの
  ルートとして独自の seal を持つ必要がある。
- 0027a で Option A または D (エージェント単位の chain) が選ばれた場合、
  プランの seal はオプションの構造的追加となる。

---

## Decision drivers

- **ADR-0023 の plan_step_completed イベント**: プランはステップ境界で WAL
  イベントをすでに emit している; これらは自然な seal 候補。
- **監査の完全性**: マルチステップのプランの監査は、独立した skill run seal の
  集合としてではなく、単位として追跡可能であるべき。
- **プランクラッシュのセマンティクス**: 実行途中でクラッシュしたプランは
  部分的な skill run seal セットを生成する; プラン自体が seal を持つかどうかが
  部分実行の検出方法に影響する。
- **seal_unit の直交性**: `seal_unit: skill` デフォルトは安定したベースラインの
  まま保たれるべき; プランレベルのシーリングは置き換えではなく拡張。
- **実装コスト**: プランレベルの seal を追加するには `PlanRuntime` の
  ライフサイクルイベント (plan start、plan complete、plan abort) へのフックが必要。

---

## 検討した Options

### Option A: プランは seal 対象外 (skill run のみ)

skill run のみをシールする。プランは各 skill run の `AuditSeal` と
`AuditContext` の `plan_id` フィールドを通じてのみ監査証跡に現れる。

プラン実行全体を再構築したい verifier は、一致する `plan_id` を持つ全 seal を
クエリする。

**Pros:**
- 既存の `seal_unit: skill` ベースラインを変更しない。
- プランコーディネーター (`PlanRuntime`) が AuditSeal を意識する必要がない。
- 最もシンプルな実装パス。

**Cons:**
- 「このプランが最初から最後まで実行された」という単一の監査成果物がない。
- 部分的なプラン実行 (ステップ 1–3 はシール済み、クラッシュによりステップ
  4–5 が欠落) の検出には、複数 seal のクロス参照と期待ステップ数の知識が必要。
- `plan_step_completed` WAL イベントに対応する seal 境界がない。

### Option B: プランが独自の seal を持つ (plan_seal) + 子 skill run がそれを参照

プラン完了時 (またはクラッシュ時) に `PlanSeal` が生成される:

```json
// audit/seals/plan-<plan_id>.json
{
  "plan_id": "plan-xyz",
  "seal_kind": "plan",
  "step_count_expected": 5,
  "step_count_completed": 5,
  "child_seals": ["run-1/seal", "run-2/seal", ...],
  "chain_hash": "sha256:...",
  "prev_seal": "sha256:..."
}
```

各子 skill run の seal がプランの seal を参照する:

```json
{
  "run_id": "abc123",
  "plan_id": "plan-xyz",
  "plan_seal_ref": "sha256:..."
}
```

**Pros:**
- プラン実行全体を表す単一の監査成果物。
- 部分完了が即座に可視: `step_count_expected` vs `step_count_completed` の不一致。
- sub-ADR 0027a の Option C (ワークフロー単位の tree) を可能にする。

**Cons:**
- `PlanRuntime` への AuditSeal ライフサイクルフックの追加が必要。
- `PlanSeal` はスキル単位の `AuditSeal` と異なる新しい成果物タイプ。
- クラッシュしたプランは完了時に `PlanSeal` を生成できない; クラッシュ時に
  (部分的な) プランの seal を書き出す必要があり、sub-ADR 0027d の writer
  失敗セマンティクスと連動する。
- `PlanRuntime` が async タスクとして実行される場合、プランの seal は全子タスク
  完了後に生成される; 子 seal との相対的な順序は sub-ADR 0027a のチェーン
  トポロジー選択に依存する。

### Option C: 各 skill run は独立; プランはメタデータ集約のみ

Option A と同じだが、verifier が `plan_id` ごとに skill run seal を集約して
論理的な「プランビュー」を提示する責任を持つ。新しい seal 成果物なし。
プランのメタデータ (期待ステップ数、グラフ構造) は seal でないマニフェスト
ファイルに別途保存される。

**Pros:**
- `AuditSeal` スキーマがシンプルなまま。
- Verifier の複雑さは書き込み時ではなくクエリ時に押し込む。

**Cons:**
- マニフェストファイルはハッシュチェーンの一部でないため、その integrity が
  暗号的に証明されない。
- Verifier が完全性を計算するためにプラン構造を知る必要があり、seal 成果物だけで
  自己完結しない。

### Option D: プランをディスパッチするエージェントが自身のチェーン上にプランを持つ

ディスパッチするエージェント (`PlanRuntime` を実行しているもの) が自身の
エージェント単位チェーン上にプラン実行のための seal を生成する (sub-ADR 0027a
の Option A または D と同じチェーン内)。異なるエージェント上で実行される子
skill run は自身のチェーンに seal を生成し、ディスパッチエージェントのプラン
レベル seal への `parent_seal_ref` を持つ。

これは sub-ADR 0027a の Option D (ハイブリッド: エージェント単位 chain +
クロスエージェント参照リンク) の拡張で、プランコーディネーターをディスパッチ
エージェントのチェーン内の「skill run」として扱う。

**Pros:**
- 新しい `PlanSeal` 成果物タイプなしでエージェント単位チェーントポロジーを再利用。
- プラン実行はディスパッチエージェントのチェーン内の seal で表現される。
- sub-ADR 0027a の推奨 Option D と整合。

**Cons:**
- ディスパッチエージェントのプラン「seal」はコーディネーターを表し、skill run
  ではない — `run_id` / `skill` フィールドがこの区別を表現する必要がある。
- プランの seal が書き込まれた後に完了する並行子 skill run は、ディスパッチ
  エージェントのチェーン内の「過去の」seal を参照することになる。

---

## Recommendation (proposed direction)

完全な監査完全性のためには **Option B (プランが独自の seal を持つ)** を推奨し、
**Option A を最小限の viable フォールバック** とする。

理由:
- Option B はプラン実行のための自己完結した監査成果物を提供する。これは
  「このワークフローが完了まで実行されたことを証明せよ」が要求クエリとなる
  エンタープライズ compliance に対する正しい答え。
- Option A は AuditSeal の最初のリリース (plan-mode が compliance コンテキストで
  広く使われる前) に受け入れ可能で、実装スコープを削減する。
- Option C は暗号的証明なしで verifier に integrity を委ねる — 長期設計として
  不適。
- Option D はプランモード表現を sub-ADR 0027a のトポロジー決定に依存する形で
  結合させ、両 ADR を制約する; デカップリングが望ましい。

**シーケンス推奨**: まず Option A (プランの seal なし、子 seal に `plan_id`
メタデータ) を実装する。`reyn audit verify` をマルチステップワークフロー向けに
実装するフォローアップ PR で Option B (PlanSeal) を追加する。Option B の実装は
sub-ADR 0027d (writer 失敗セマンティクス) の解決でゲートする — クラッシュ時の
`PlanSeal` には定義済みの部分シールの振る舞いが必要なため。

本 recommendation は実装着手時に再判断すること。

---

## Open questions

1. Option B について: エージェント単位チェーン内での `PlanSeal` と最後の子
   skill run seal の seal 順序は? (プランの seal は全子タスク完了後に書かれる;
   同じエージェントの他の並行プランからの seal と交互になる可能性がある。)
2. プランステップがリトライされた場合 (ADR-0022/0023 による子クラッシュと
   リカバリー後)、リトライは失敗した試行の seal と並んで新しい子 seal を生成
   するか、それとも失敗した試行の seal が修正されるか?
3. `plan_step_completed` イベント (ADR-0023 §3.2) は中間の seal として表現される
   べきか (`seal_unit: plan_step`)、それとも WAL のみのイベントとして対応する
   seal なしで残るか?
4. `PlanSeal` の `step_count_expected` フィールドについて: プラングラフは
   動的 (LLM がステップをスキップまたは追加できる); プラン開始時に期待数は
   どのように確立されるか?

---

## Related

- ADR-0027: AuditSeal 分離 (親 ADR)
- ADR-0027a: ハッシュチェーントポロジー (直接連動 — トポロジー選択がこの
  sub-ADR の Option D の実現可能性に影響)
- ADR-0027b: config_hash スコープ
- ADR-0027d: writer 失敗時のセマンティクス (クラッシュ時の PlanSeal はこれに依存)
- ADR-0023: Plan-Mode Forward Replay (プラン実行モデルと WAL イベント)
- ADR-0022: Plan-Mode Crash Fail-Safe (plan-mode のクラッシュリカバリー)
