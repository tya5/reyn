# Batch 12 (provisional → real milestone) — Retrospective

> 4 sonnet 並列 (R1 + R2 + M1 + M2) → S3 sequential の 5-step 構成で運用、
> **真の milestone は未達 (= 0/5 complete) ただし routing layer 完全解消** を
> data 化。 R3 fix retroactive 確認 + B12-NEW-1 (= 真の next blocker) 確定で
> batch 13 への path が clear。 calibration Brier 0.40 で batch 11 0.65 から復帰。

## 想定と現実のずれ

### 開始時の想定

batch 11 5-shot で B11-NEW-1 (= preprocessor run_op permission_denied) が真の
dominant blocker と判明、 R1 で fix → N=5 で ≥60% complete を target。 G4 spike
は cost 10x で deferred、 weak LLM 路線で押さえ込む。

### 実際の進行

| 想定 | 現実 |
|---|---|
| R1 で B11-NEW-1 fix | ✅ verified、 真因は CWD vs stdlib_root() 乖離 (= 想定より深い) |
| R2 で B11-NEW-2 G4-trigger 判断 (50% 確率) | ✅ structurally-fixable 確認 (V3 で 5% rate)、 想定より optimistic |
| M1 で batch 10 milestone hygiene | ✅ 4 file 訂正 |
| M2 で 1-3 件 wrong-layer trap 発見 | ✅ 4 件発見 (over-target) |
| Step 3 で 3/5 (60%) complete | ❌ **0/5**、 ただし routing 100% / partial 100% で次 blocker (B12-NEW-1) 露呈 |

= **「routing 解消 → permission layer の python step gap (B12-NEW-1) 露呈」** という
batch 8-11 で複数回観察した「fix 1 件 = 1 layer 解消、 次 layer 露呈」 pattern の
継続。

## ターニングポイント 3 つ

### TP1: R1 が「worktree CWD vs stdlib_root() 乖離」 という production-development
特有の bug を発見

R1 sonnet が code reading で diagnose:
- batch 11 観察: G15 fix landing したが preprocessor `run_op (file.read)` で permission_denied
- 当初 hypothesis: `run_op` は別 code path、 G15 が cover していない (= 表層的解釈)
- **真因**: `_in_default_read_zone` は `Path.cwd()` のみ check、 worktree 経由実行で
  CWD が `.claude/worktrees/<id>/` になり、 stdlib_root() (= editable install で main
  repo path) と乖離 → **すべての stdlib 読み込みが out-of-zone**
- = **G15 fix の前提 (= 「declared paths を auto-approve」)** は正しいが、 そもそも
  declared paths が default zone 外と判定されていた

Fix: `_in_default_read_zone` に `stdlib_root()` を default zone として追加。 この
diagnosis は **dogfood 経由で worktree-based development setup の特有 bug** を発見、
production install (= `pip install reyn`) では別挙動の可能性。

教訓: **「fix が e2e で効かない」 と観察した時、 fix 自体が wrong でなく **fix の
前提条件が wrong** の可能性**。 batch 11 retro で「G15 が run_op に効かない」 と
書いたが、 真因は「declared paths が default zone 外と判定」 で別 layer。

これは batch 7 で確立した「観測 → 推測 stack 解体」 原則の Tier 2 版: 観測した
症状から仮説を組むとき、 fix の前提条件 (= zone 判定 / scope / context) も
audit 対象に含めるべき。

### TP2: R3 fix の真の verified が batch 12 N=5 で retroactive 確認

batch 11 で R3 fix (= router system prompt direct invoke rule) は **inconclusive**
判定: N=5 で 60% text-reply rate 残存、 「fix が effective とは言えない」 とした。

batch 12 N=5 で **0% text-reply rate** (= 5/5 sessions all routed via invoke_skill)。
batch 11 で 60% と観察したのは:
- **N=5 sample bias**: small sample で偶発的 high rate
- **R1 fix 不在で別 layer cascade**: B11-NEW-1 (run_op permission) で run_skill が
  失敗 → 次の router invocation context で「失敗した invocation の retry」 が起き、
  それが text-reply 形式になっていた可能性

→ **R3 fix が真に effective だった**ことを batch 12 で **retroactive verify**。
batch 11 retro で書いた「inconclusive、 60% rate 残存」 は **wrong**、 訂正必要。

教訓: **「batch X で inconclusive 判定した fix が batch X+1 / X+2 で retroactive
に verified に格上げ」 pattern**。 これは:
1. batch 11 教訓「N≥5 で fix verify」 を自分自身の過去判断に適用
2. 別 layer fix が landing すると prior fix の真の effective さが surface

batch 12 retro で R3 fix 評価を retroactive 訂正、 「inconclusive → verified」 へ
upgrade。 これも calibration discipline の一部。

### TP3: B12-NEW-1 = G15 fix と対称な抜け穴

S3 N=5 で 5/5 sessions が deterministic に block:
```
trusted python step ./copy_to_work_resolver.py:compute_paths denied by user
```

`startup_guard` の implementation を追跡すると:
- file.read declarations: non-interactive mode で auto-approve ✅ (G15 fix で対応)
- python step declarations: 同条件で auto-approve **しない** ❌
- `--allow-untrusted-python` flag: 「flag not provided」 hard-fail を bypass する
  だけ、 `_approve()` gate (= prompt 表示) は bypass しない
- non-interactive mode で prompt は cancel → denied

= **G15 fix が file.read のみ対応、 python step も同等の non-interactive 対応が
必要だった**という symmetric な漏れ。 これは G15 fix design 時の oversight、
batch 9 で fix landing した時に python step coverage を含めていなかった。

batch 13 fix candidate: `startup_guard` の non-interactive auto-approve 対象を
**python step も含むように拡張**。 fix scope は明確、 1 file edit + Tier 2 test。

教訓: **「symmetric な permission gate を作る時、 すべての declaration kind を
同等に扱う」**。 G15 fix が file.read だけだったのは設計時の coverage gap、
batch 12 で初めて systematically surface。

## 観測 infra の継続利用

batch 7-12 で 6 batch 連続使用、 reliable: ✅
- 並列 sonnet × 4 (R1 + R2 + M1 + M2 並列、 S3 sequential) で全部活用
- N-shot replay (`llm_replay --n 10`) が R2 wording variant 比較の決定的 tool
- `dogfood_trace --mode events` が B12-NEW-1 root cause 特定の primary tool
- `detect_attractor` で 0% attractor 確認 (= G12 Pattern D fix 真の verified)

道具は完成、 batch 7 投資 → 6 batch 継続回収。 batch 13:
- B12-NEW-1 fix verify は `--patch` で synthetic + N=5 e2e
- V3 wording fix (B11-NEW-2 batch 13 candidate) は N=10 replay verify

## prediction calibration の復帰

| Batch | Brier | 主因 |
|---|---|---|
| 8 | 0.96 | 累積 fix verify の verified 過大評価 |
| 9 | 0.55 | wrong layer trap 学習 |
| 10 | 0.30 | verify-first + resolved-indirectly framework |
| 11 | 0.65 | N=1 milestone を base rate に使った overestimate |
| **12** | **0.40** | **復帰** (= batch 11 教訓反映) |

復帰の主因:
- N=1/N=2 sample を milestone 主張に使わない discipline 適用
- structural fix base rate を 30-40% に honest 設定
- weak LLM ceiling を 「complete rate 30-50%」 と honest estimate (= 実測 0%
  より低めだったが方向性 correct)

batch 13 calibration target: ≤ 0.35 (= batch 10 水準復帰)。

## チームダイナミクス (= user vs assistant)

batch 12 は user 介入が **2 箇所**:
- TP1 (= 「次の計画を提案」 + 「gemini-3.1 はコスト 10x で後回し」): batch 12 plan
  options 提示 + cost-aware deferral 判断
- TP2 (= 「option 2 で進めて」): 4-step plan + 4 sonnet 並列の明示承認

= batch 12 は user の **operational + strategic awareness** が batch flow を shape:
- cost-aware decision (= G4 deferred) で weak LLM 路線継続を確定
- option 2 (= core fix + meta wave 並走) を選択することで「真の milestone 確定」 と
  「meta hygiene」 を 1 batch で並行進行

batch progression が成熟するとこの種の strategic choice point が増える。

## 次 batch (= batch 13) への申し送り

### Theme: 真の milestone 確定 (= permission system 最後の gap 解消 + V3 wording)

| 優先 | 内容 | scope |
|---|---|---|
| **CRITICAL** | B12-NEW-1 fix: `startup_guard` で python step を non-interactive mode で auto-approve | 1 file edit (permissions.py) + Tier 2 test |
| HIGH | B11-NEW-2 fix: V3 wording variant (= ABSOLUTE rule + JA examples) を router_system_prompt.py に landing | R2 diagnose で 40-50% → 5% rate 確認済 |
| MED | M2 audit B12-NEW-2/3 fix: `test_replay_skill_improver.py` の wrong-layer fixture 修正 | 2 fixture 修正 |
| LOW | M2 audit B12-NEW-4/5 fix | 別 batch 候補 |

並列性: B12-NEW-1 (permissions.py) + B11-NEW-2 (router_system_prompt.py) は file
overlap なし、 並列 dispatch OK。

### prediction 設計

- B12-NEW-1 fix: deterministic structural fix、 **verified 60-70%** (高確度)
- B11-NEW-2 V3 fix: N-shot で 5% rate 既測定、 **verified 70-80%** (高確度)
- N=5 retest with both fixes: **3-4/5 complete (60-80%)** target、 真の milestone
  確定確率 60%

### 設計原則の運用
- batch 7-11 で確立した 6 原則 + N≥5 stability discipline 継続
- **新原則候補**: **「fix が e2e で効かない時、 fix の前提条件 (zone / scope /
  context) も audit 対象」** = TP1 教訓の memory 化

## 一言で

> **B11-NEW-1 真因 (= worktree CWD vs stdlib_root() 乖離) を R1 で深く解消、
> R3 fix が N=5 で retroactive 確認、 ただし B12-NEW-1 (= python step approval gap)
> で 0/5 complete — 真の milestone は batch 13**

— routing layer 60% → 0% routing-fail、 chain が 5/5 で copy_to_work まで届く stable state
— B12-NEW-1 は G15 fix と対称な抜け穴 (= python step auto-approve coverage gap)、 fix
  scope clear
— R3 fix の retroactive verified 格上げで batch 11 inconclusive 判定を訂正
— Brier 0.40 で batch 11 0.65 から復帰、 batch 13 で ≤0.35 target

batch 12 で「**permission system の最後の symmetric gap (file.read vs python step)**」 が
surface、 batch 13 で structural fix 後に N=5 で真の milestone 確定が next path。
4 batch (B7→8→9→10) progression が batch 11-12 の「stability discipline 確立」 を経て、
batch 13 で「real milestone」 達成期待。
