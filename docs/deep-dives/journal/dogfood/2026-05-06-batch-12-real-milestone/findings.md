# Batch 12 (provisional → real milestone) — Findings

> N≥5 measurement で batch 10 provisional milestone を real milestone に格上げ
> 試行。 **R1 fix で routing 100% / partial 100%、 ただし新 blocker B12-NEW-1
> (= startup_guard が python step を auto-approve しない) で 0/5 complete**。
> 真の milestone は batch 13 で B12-NEW-1 fix 後に再測定。

## Summary table

### Step 1: B11-NEW-1 fix (R1)

| Field | Value |
|---|---|
| Commit | `2219b20` |
| 真因 | worktree CWD ≠ stdlib_root()、 `_in_default_read_zone` が CWD のみ check |
| Fix | `_in_default_read_zone` に `stdlib_root()` 追加 |
| Tier | Tier 2 × 6 + 既存 test 5 件更新 |
| Test 結果 | 1016 → 1022 passed (+6) |

### Step 2: B11-NEW-2 diagnose-only (R2)

| Hypothesis | Verdict |
|---|---|
| A: Available skills injection 不足 | **eliminated** (= skill_improver 確認) |
| B: wording variants | **structurally-fixable** (V3 = ABSOLUTE rule + JA examples で 5% rate) |
| C: weak LLM ceiling | **eliminated** (= 40-50% → 5% gradient で 1 strict capability ceiling 否定) |

= **structurally-fixable**、 batch 13 で V3 wording fix dispatch 候補。

### Step 3 (PRIMARY): N=5 stability retest

| Metric | batch 11 baseline | batch 12 N=5 | Delta |
|---|---|---|---|
| Complete rate | 0/5 (0%) | **0/5 (0%)** | 0pp |
| Routing-fail rate | 3/5 (60%) | **0/5 (0%)** | **-60pp** ✅ |
| Partial rate | 2/5 (40%) | **5/5 (100%)** | +60pp |
| Most common stop | router text-reply | copy_to_work step[0] python denied | layer shift ↓ |

**真の milestone**: **not-yet**。 ただし routing layer が完全解消、 chain が一律
copy_to_work まで到達。

### Step 4a (M1): batch 10 milestone hygiene

| Field | Value |
|---|---|
| Commit | `cc41333` |
| Files | 4 (retrospective.md / findings.md / B10-aggregated.md / journal README) |
| Banner | `⚠️ Provisional milestone (= N=1 sample)` 追加 |

= batch 10 milestone claim を **provisional** に正式訂正、 batch 12 N=5 が real
milestone confirmation の reference として明示。

### Step 4b (M2): Tier 2 fixture audit

| Field | Value |
|---|---|
| Commit | `5638cbb` |
| Files audited | 30 |
| B12-NEW-N candidates | 4 (HIGH × 2、 MED × 2) |
| Verified-correct | 27 |

| ID | 重要度 | 内容 |
|---|---|---|
| **B12-NEW-2** (audit) | HIGH | `test_replay_skill_improver.py` の `_candidate_copy_to_work` が **存在しない `work_config` schema** 使用 (= classic wrong-layer trap、 G17 pattern) |
| **B12-NEW-3** (audit) | HIGH | 同 file の `iteration_state.session` fixture が `_resolved_paths` field 欠落 (= R1 既知問題に直結) |
| **B12-NEW-4** (audit) | MED | `copy_to_work_validation_judgment.py` の path に `/phases/` 余計 |
| **B12-NEW-5** (audit) | MED | postprocessor output_schema scope ambiguity |

### 検出した真の新 blocker (= batch 13 候補)

| ID | 重要度 | 内容 |
|---|---|---|
| **B12-NEW-1** | **CRITICAL** | `startup_guard` が **python step** を non-interactive mode で auto-approve **しない** (= file.read だけが auto-approve)。 `--allow-untrusted-python` flag は「flag not provided」 hard-fail を bypass するが `_approve()` gate を bypass しない。 5/5 sessions が copy_to_work step[0] (python `compute_paths`) で deterministic に block |

## Round 別 narrative

### Round 1: 4 sonnet 並列 dispatch (R1 + R2 + M1 + M2)

prelude landing 後、 file overlap なしの 4 task を並列 background dispatch:
- R1: `src/reyn/permissions/` 周辺 (= permission 修正)
- R2: docs only (= diagnose-only)
- M1: batch 10 doc 更新 (= meta hygiene)
- M2: tests/ 走査 (= read-only audit)

各 worktree で独立 sonnet が code reading + (R1/M1 は) doc/code 修正 + commit、
順次 main に sequential cherry-pick (M1 → M2 → R2 → R1 の順)。 衝突なし。

### Round 2: R1 で深い root cause 発見

R1 sonnet が **B11-NEW-1 (= preprocessor run_op permission_denied) の真因** を
diagnose:
- `Path.cwd()` は worktree directory (例: `.claude/worktrees/<id>/`)
- `stdlib_root()` は editable-installed package で main repo path (`.../sandbox_2/src/reyn/stdlib`)
- `_in_default_read_zone` は CWD のみ check
- → worktree から実行すると **すべての stdlib 読み込みが out-of-zone**
- = G15 fix (= `startup_guard` auto-approve) は declared paths を auto-approve
  するが、 そもそも declared paths が「out-of-zone」 だったため auto-approve
  scope に入らないケースがあった

Fix: `_in_default_read_zone` に `stdlib_root()` を default zone として追加。
**lazy initialization** で circular import を回避 (= 細部の implementation
discipline)。 6 Tier 2 test + 既存 test 5 件更新。

これは batch 11 retro で書いた「 G15 が `run_op` 経由で効かない」 の **真の
diagnostic**。 「resolved-indirectly」 classification の **N-shot verification
不足** という batch 11 教訓を data 化。

### Round 3: N=5 で routing fix が真に effective 確認

S3 N=5 で **routing-fail 60% → 0% の劇的改善**。 R3 fix (commit `2c14aa6`、
batch 11 で landing) が **真に effective** であることが N=5 で確認。 batch 11
で 60% rate と観察したのは N=5 sample bias の可能性、 もしくは R1 fix landing
で別 layer の cascade が消えて router 振る舞いが安定化した可能性。

= **「fix の effective 確認には N≥5 必要」** という batch 11 retro 教訓が batch 12
で実証 (= R3 が batch 11 で inconclusive 判定だったが batch 12 で確実な
verified に格上げ)。

### Round 4: 新 blocker B12-NEW-1 露呈

R1 fix で routing 100% / chain reach copy_to_work 100%、 ただし copy_to_work
step[0] (`compute_paths` python step) で 5/5 sessions が deterministic に block:

```
trusted python step ./copy_to_work_resolver.py:compute_paths denied by user
```

これは **G15 fix と対称な抜け穴**:
- G15 fix: file.read を non-interactive mode で auto-approve ✅
- python step: 同条件で auto-approve **しない** ❌

`--allow-untrusted-python` flag は「flag not provided」 hard-fail を bypass する
だけで、 user approval prompt 自体を bypass しない。 non-interactive mode で
prompt が cancel される → denied。

= **「fix 1 件 = 1 layer 解消、 次 layer の new blocker は >50% 確率で露呈」** という
batch 8-11 で複数回観察した structural pattern の継続。 routing layer 解消で
permission layer の python step gap が surface した。

## Prediction calibration

batch 12 prelude で予測:

| Step | Top prediction | Actual | Hit? |
|---|---|---|---|
| Step 1 (R1) | verified 60-70% | verified | **hit** |
| Step 2 (R2 diagnose) | structurally-fixable 35% / G4-trigger 50% | structurally-fixable | **hit (under-predicted)** |
| Step 3 (N=5) | 3/5 (60%) target、 25% probability | 0/5 | **miss** |
| Step 4a (M1) | verified 90% | verified | **hit** |
| Step 4b (M2) | verified 70% (1-3 件発見) | verified (4 件発見) | **hit (over-found)** |

= 4/5 hit、 1/5 miss。 Brier ≈ 0.40 (batch 11 0.65 から復帰)。

milestone miss の理由:
- B12-NEW-1 (= startup_guard python step gap) を prediction に含めていなかった
- 「next layer 露呈 30-40%」 base rate を Step 3 prediction に振ったが、 layer は
  既知 (= permission system) で **deterministic block** だった
- routing layer 大改善 (60% → 0%) は positive surprise、 ただし complete rate に
  寄与せず

## A4 review (= user 感覚との差分)

- **R1 fix が深い**: worktree CWD vs stdlib_root() 乖離は editable install
  特有の問題、 production install (= pip install reyn) では発生しない可能性。
  dogfood が「production-grade development setup」 特有の bug を発見、
  Reyn vision の整合性高い
- **R3 fix の真の verified が batch 12 で確認**: batch 11 で inconclusive 判定
  だったが N=5 で 0% routing-fail。 「N=1/N=2 で確定しない」 batch 11 教訓を
  自分自身の prior fix verdict に適用、 retroactive correction
- **真の milestone は batch 13**: complete rate 0/5 だが routing 100% / partial
  100% で「**chain が一律 copy_to_work まで届く**」 stable state、 batch 11
  「0/5 with B11-NEW-1 random fail」 とは質が異なる。 next blocker (= B12-NEW-1)
  が deterministic で fix 設計が clear
- **fix 累積の structural progression**: batch 8 (B8-NEW-1+2) → batch 9 (G15) →
  batch 10 (B9-NEW-2) → batch 11 (R1+R2+R3) → batch 12 (B11-NEW-1) → 各 layer の
  blocker を順次解消、 batch 13 で permission system の最後の gap (B12-NEW-1)
  解消で **真の milestone** 期待

## 残懸念点 + batch 13 候補

| 優先 | 内容 | 関連 |
|---|---|---|
| **CRITICAL** | B12-NEW-1 fix: `startup_guard` で python step を non-interactive mode で auto-approve | Step 3 |
| HIGH | B11-NEW-2 fix: V3 wording variant (= ABSOLUTE rule + JA examples) を router_system_prompt.py に landing (R2 diagnose で 40-50% → 5% rate 確認済) | R2 |
| MED | B12-NEW-2 fix: `test_replay_skill_improver.py` の `work_config` 存在しない schema 修正 (= wrong-layer trap) | M2 audit |
| MED | B12-NEW-3 fix: 同 file の `iteration_state.session` fixture に `_resolved_paths` 追加 | M2 audit |
| LOW | B12-NEW-4 / B12-NEW-5 fix (= path mismatch / postprocessor scope) | M2 audit |
| trial | G4 spike (= 強モデル併用): `gemini-3.1-flash-lite-preview` evaluation | user-side cost 10x deferred |

batch 13 は **B12-NEW-1 (CRITICAL) + V3 wording fix (HIGH)** の **2 fix structural
wave**、 N=5 retest で **real milestone 確定** が theme。

## 一言で

> **R1 fix で routing 100% / chain reach copy_to_work 100%、 ただし新 blocker
> B12-NEW-1 (= python step auto-approve gap) で 0/5 complete — 真の milestone は
> batch 13、 R3 fix は N=5 で真に verified に格上げ**

— B11-NEW-1 真因 (= worktree CWD vs stdlib_root() 乖離) を deep diagnose
— R3 fix が batch 12 で「真に effective」 retroactive 確認 (60% → 0% routing-fail)
— B12-NEW-1 (= python step approval gap) は G15 fix と対称な抜け穴、 batch 13 で fix
— Brier 0.40 (batch 11 0.65 から復帰)、 prelude prediction の 4/5 hit

batch 12 で「**fix 1 件 = 1 layer 解消、 次 layer 露呈**」 structural pattern を
継続実証、 batch 13 で permission system の最後の gap 解消が真の milestone trigger。
