# FP-0008: SWE-bench 参加インフラ — stdlib スキル + バッチ実行

**Status**: partially superseded — Component A（`swe_bench` スキル）は #187 で退役 / Component B（`reyn eval benchmark`）は実装済
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

> **Status update（#187 以降）。** Component B — `reyn eval benchmark` バッチランナー
> — は実装され現行。Component A — `swe_bench` stdlib スキル — は実装後 **#187 で退役**:
> 現在の SWE-bench 実行方式は各 instance を **general agent 経由 `reyn run-once`**（スキル
> なし、prompt に held-out `test_patch` なし）でルートし、per-instance に
> `scripts/swe_bench_runner.py` がラップする。authoritative スコアリングは外部
> `swebench` harness（`eval_benchmark.run_tier1_swebench_eval`）へ委譲。以下のスキル
> ベース設計（Component A）は当初提案の記録として保持する。
> **今 SWE-bench を実際に回す手順は operator how-to を参照:
> [Run SWE-bench](../../guide/for-reyn-developers/run-swe-bench.md)。**

## Summary

Reyn が SWE-bench Verified（コーディング agent ベンチマークのデファクト標準）に
参加できるようにする。必要な能力（file edit / shell / git）は既存 op で揃っており、
追加するのは (A) `swe_bench` stdlib スキルと (B) `reyn eval benchmark` バッチ実行コマンドの 2 つ。

---

## Motivation

### SWE-bench の業界的位置付け

SWE-bench Verified は 2026 年時点でコーディング agent のデファクト評価基準。
主要フレームワーク・モデルベンダーがスコアを競っており、
**Reyn が参加できること自体が OSS ローンチ時の信頼性の証明になる**。

Claude Opus 4.7 が 87.6%（2026-04）でほぼ上限に達しているため、
モデル性能より「Reyn のアーキテクチャが本番コーディングタスクで機能するか」の
実証として使える。

### Reyn の既存能力で参加できる

SWE-bench が要求する能力:

| 要求 | Reyn の対応 |
|---|---|
| コードファイルの読み取り | `read_file` op ✅ |
| ファイル編集 | `edit_file` op ✅ |
| テスト実行 | `shell` op ✅ |
| git diff の取得 | `shell` op (`git diff`) ✅ |
| リポジトリ grep | `grep` op ✅ |

OS 変更不要。スキルとして実装できる（P7 遵守）。

---

## SWE-bench の仕組み

```
SWE-bench harness
  → タスクデータを渡す (instance_id, repo, base_commit, problem_statement)
  → Reyn が swe_bench スキルを実行
  → git patch (diff) を出力
  → harness が patch を apply してテスト実行
  → pass / fail を判定
```

> ⚠️ **Superseded（歴史的記録）。** 上記の「Reyn が swe_bench スキルを実行」ステップは
> #187 で退役。現在のフローは per-instance コンテナ内で **general agent 経由 `reyn run-once`**
> を実行（スキルなし）し、harness が patch を apply して外部スコアリングする。
> [operator how-to](../../guide/for-reyn-developers/run-swe-bench.md) を参照。

Reyn の呼び出し口:

```
# 単一タスク
reyn run swe_bench --input instance.json --output patch.diff

# バッチ（500 問）
reyn eval benchmark swe_bench --tasks swe_bench_verified.jsonl --output results/
```

---

## Proposed implementation

### Component A — `swe_bench` stdlib スキル（MEDIUM） — #187 で退役

> **退役済。** このスキルは実装後 #187 で退役し、agent-routed runner（general agent
> 経由 `reyn run-once`、スキルなし）へ置換された。以下の phase/スキル設計は当初提案の
> 記録としてのみ保持する。

```
src/reyn/stdlib/skills/swe_bench/
  skill.md
  phases/
    setup.md          ← リポジトリを base_commit にチェックアウト
    explore.md        ← problem_statement を読み、関連コードを grep で特定
    plan.md           ← 修正方針を決定
    apply.md          ← edit_file / write_file で変更を実装
    verify.md         ← shell で失敗テストを実行し、通過を確認
    report.md         ← git diff を取得し最終出力を整形
```

**skill.md frontmatter の骨格**:

```yaml
---
name: swe_bench
description: SWE-bench タスクを解く — GitHub issue のコード修正と検証
entry_phase: setup
graph:
  setup:     [explore]
  explore:   [plan]
  plan:      [apply]
  apply:     [verify, plan]   # テスト失敗なら plan に戻る
  verify:    [report, apply]  # 検証失敗なら apply に戻る
  report:    []               # 終了
final_output_schema: swe_bench_result
input_schema:
  instance_id: string
  repo: string
  base_commit: string
  problem_statement: string
  hints_text: string          # optional
  test_patch: string          # 評価用テスト（実行のみ、編集不可）
permissions:
  file:
    read: ["*"]
    write: ["*"]              # リポジトリ全体への書き込みが必要
  shell: true                 # git / テストランナー実行
---
```

**各フェーズの役割**:

`setup` — リポジトリを base_commit にチェックアウトし、
テスト環境を準備する（shell op で `git checkout <base_commit>`）。

`explore` — `problem_statement` から修正すべきファイル・関数を特定。
`grep` op で関連コードを検索し、`read_file` で文脈を収集する。
結果を workspace の `exploration.md` に保存。

`plan` — exploration 結果をもとに修正計画を立案。
変更対象ファイル・修正内容を `plan.md` に保存。

`apply` — `plan.md` に従い `edit_file` / `write_file` で変更を実施。
1 ファイルずつ修正し、構文エラーがないか基本確認。

`verify` — `test_patch` のテストを `shell` op で実行。
全テスト通過 → `report` へ。失敗 → `apply` に戻る（最大 `max_retries: 3`）。

`report` — `git diff HEAD` を実行して patch を生成。
SWE-bench が期待するフォーマットで `final_output` に格納。

**final_output_schema**:

```python
class SweBenchResult(BaseModel):
    instance_id: str
    patch: str          # git diff の出力
    tests_passed: bool
    attempts: int       # verify ループの回数
```

### Component B — `reyn eval benchmark` バッチ実行コマンド（MEDIUM） — 実装済

SWE-bench Verified の 500 問を効率的に実行するバッチランナー。

> **実装済（現行）。** `reyn eval benchmark <SKILL> --tasks … --output …` は実在し、
> バッチドライバとして稼働。スキルを JSONL タスクファイルに対し並行ディスパッチで実行し、
> Tier-1 faithful スコアリングを内蔵する（SWE-bench タスクは `--clone-task-repo`）。
> [`reyn eval` reference](../../reference/cli/eval.md#subcommand-benchmark) と
> [operator how-to](../../guide/for-reyn-developers/run-swe-bench.md) を参照。

```
reyn eval benchmark <skill_name> \
  --tasks swe_bench_verified.jsonl \
  --output results/ \
  --concurrency 4 \
  [--limit 50]              # 先にサブセットで試す
  [--resume]                # 途中から再開
```

**入力 JSONL フォーマット**（SWE-bench 公式データセットのフォーマットをそのまま使用）:

```jsonl
{"instance_id": "django__django-1234", "repo": "django/django", "base_commit": "abc123", "problem_statement": "...", "hints_text": "...", "test_patch": "..."}
```

**出力ディレクトリ構造**:

```
results/
  run_<timestamp>/
    summary.json          ← 全体の pass rate / 実行時間 / コスト集計
    patches/
      django__django-1234.diff
      ...
    logs/
      django__django-1234.jsonl  ← P6 イベントログ（per instance）
```

**`--resume` の動作**:
`results/run_<timestamp>/summary.json` から完了済み instance_id を読み取り、
未実行分のみ実行する（実行中断後の再開）。

**summary.json フォーマット**:

```json
{
  "run_id": "run_20260510_093000",
  "skill": "swe_bench",
  "total": 500,
  "completed": 423,
  "passed": 371,
  "pass_rate": 0.877,
  "total_cost_usd": 142.30,
  "avg_cost_per_instance": 0.34,
  "avg_attempts": 1.8
}
```

### SWE-bench harness との接続

SWE-bench の公式評価は Docker コンテナ内で実行される。接続方法:

**方法 1: CLI 直接実行（推奨）**

```bash
# SWE-bench harness から呼び出す wrapper script
reyn run swe_bench \
  --input '{"instance_id": "...", "repo": "...", ...}' \
  --output-field patch \
  > patch.diff
```

**方法 2: A2A エンドポイント経由**

```
reyn web  # localhost:8080 で起動
# harness が POST /a2a/agents/swe_bench に message/send
```

A2A エンドポイント（`reyn web`）は既存実装のため追加変更なし。

---

## FP-0007 との関係

| FP | 関係 |
|---|---|
| FP-0007（評価インフラ）| `reyn eval benchmark` のバッチ runner は Component B（`reyn eval run`）の拡張版。`reyn eval benchmark` が N 問を、`reyn eval run` が 1 スキル × M テストケースをそれぞれ担当 |
| FP-0007 Component A（export）| バッチ実行の P6 ログを Langfuse に export することで、どのフェーズで失敗が多いかの可視化が可能 |

---

## Dependencies

- `src/reyn/stdlib/skills/` — `swe_bench/` 追加（OS 変更なし）
- `src/reyn/cli/eval.py` — `benchmark` サブコマンド追加（FP-0007 Component B と同ファイル）
- `src/reyn/op_runtime/shell.py` — shell op（既存、変更なし）
- FP-0007: `reyn eval benchmark` は FP-0007 の eval.py と同ファイルに実装するため
  同時または FP-0007 後にリリースが望ましい。ただし独立実装は可能。

前提 PR: なし（swe_bench スキルは OS 変更不要で独立実装可能）。

---

## Cost estimate

**合計: LARGE**

| タスク | コスト | 備考 |
|---|---|---|
| Component A: `swe_bench` スキル（6 フェーズ）| MEDIUM | フェーズ設計 + 各 phase instruction の調整 |
| Component A: `apply` / `verify` のループ調整 | MEDIUM | retry 上限・regression 検出の動作確認 |
| Component B: `reyn eval benchmark` CLI | MEDIUM | concurrency / resume / summary.json 出力 |
| SWE-bench harness との接続検証 | SMALL | CLI wrapper + A2A 接続テスト |
| **合計** | **LARGE** | ボトルネックは verify ループの品質（pass rate に直結）|

---

## 期待成果

| 指標 | 目標 |
|---|---|
| Pass rate (SWE-bench Verified) | 40%+ （frontier モデル使用時、Hermes 相当）|
| コスト / instance | $0.30〜0.50（flash モデル使用時）|
| OSS ローンチへの効果 | 「Reyn で SWE-bench を回した」という実績がエコシステム信頼に寄与 |

---

## Related

- `src/reyn/stdlib/skills/skill_improver/` — 複数フェーズスキルの実装参考
- `src/reyn/op_runtime/shell.py` — shell op（git / テストランナー実行に使用）
- FP-0007 (`0007-evaluation-infrastructure.md`) — eval CLI と export の共通基盤
- FP-0006 (`0006-skill-self-improvement.md`) — swe_bench スキル自体を自己改善する将来パス
- [SWE-bench Verified](https://www.swebench.com/)
- [SWE-bench GitHub](https://github.com/princeton-nlp/SWE-bench)
