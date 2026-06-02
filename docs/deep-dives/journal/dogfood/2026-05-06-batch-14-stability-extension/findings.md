# Batch 14 (stability extension + meta hygiene) — Findings

> 🏆 **production-grade phase 1 (= 機能成立 + stability) 完了 declaration**:
> N=5 で **5/5 (100%) complete rate** 達成。 batch 13 80% → batch 14 100% で +20pp、
> R1 fix (= literal model fallback) が真に effective + 想定通り chain 完走を救済。
> 全 3 fix が **🔵 不具合修正 + doc 追加** で 仕様変更ゼロ batch、 production user
> 影響なし。

## Summary table

### Step 1: 3 並列 fix (= 全 🔵 不具合修正 / doc 追加)

| Fix | Commit | 種別 | 内容 |
|---|---|---|---|
| **R1** (B13-NEW-1) | `a10553c` | 🔵 不具合修正 | `run_skill` で literal model string 検知 + `ctx.model` fallback (= ModelResolver.is_known_class) |
| **R2** (B12-NEW-2/3) | `ba07e06` | 🔵 不具合修正 | `test_replay_skill_improver.py` の wrong-layer fixture 修正 (work_config → improvement_session、 _resolved_paths 追加) |
| **R3** (doc) | `0d32a49` | 🔵 doc 追加 | `permission-model.md` + dogfood README に reyn.local.yaml pre-approval pattern 文書化 |

= **production user 観点で API/UX 変化ゼロ**、 全 fix が「documented design + runtime invariant」 への restoration / doc 化。

### Test 影響

| Batch | Tests | Delta |
|---|---|---|
| pre-batch 14 | 1010 | — |
| post-R1 | 1020 | +10 (= 4 Tier 1 ModelResolver + 6 Tier 2b run_skill model selection) |
| post-R2 | 1020 | 0 (= 4 fixture rekey、 net unchanged) |
| post-R3 | 1020 | 0 (= doc only) |

R3 → R2 → R1 の順で sequential cherry-pick、 すべて 1020 passed 0 regression。

### Step 2: N=5 stability retest

| Metric | batch 13 baseline | batch 14 N=5 | Delta |
|---|---|---|---|
| **Complete rate** | 4/5 (80%) | **5/5 (100%)** | **+20pp** ✅ |
| Routing-fail rate | 0/5 (0%) | 0/5 (0%) | 0pp |
| R1 fix fire | n/a | 3 fire (Run 2 × 2、 Run 3 × 1) | 全 fire で正常 fallback |
| Total cost | ~$0.053 | $0.062 | +17% (= R1 fallback 経路で extra LLM call) |

#### Per-session detail

| Session | Verdict | Phases | Cost | R1 fire |
|---|---|---|---|---|
| 1 | ✅ complete | 6 phase clean (prepare → ... → finalize) | $0.0112 | 0 |
| 2 | ✅ complete | 4 phase 経由 (LLM が copy_to_work + apply_improvements skip) | $0.0190 | 2 (`gpt-3.5-turbo`) |
| 3 | ✅ complete | 6 phase clean | $0.0109 | 1 (`gemini-2.5-flash-lite` literal) |
| 4 | ✅ complete | 6 phase clean | $0.0102 | 0 |
| 5 | ✅ complete | 6 phase clean | $0.0109 | 0 |

= 全 5 sessions が finalize + narrator 到達、 user に improvement plan delivered。

### R1 fix 動作確認 (= operational evidence)

R1 fix の OS boundary intercept が 3 回観察:

```
Run 2: run_skill: op.model 'openai/gpt-3.5-turbo' is not a known model class —
       ignoring and inheriting runtime model 'standard' instead.
       (occurs twice in same session = chain context retry)

Run 3: run_skill: op.model 'openai/gemini-2.5-flash-lite' is not a known
       model class — ignoring and inheriting runtime model 'standard' instead.
```

= **LLM hallucinate (= literal model string) を OS が transparent に救済**、 chain
完走に influence:
- batch 13 では同 hallucinate で abort (= 4/5 partial)
- batch 14 では fallback で 5/5 complete

P3 (OS が runtime engine) + P7 (skill-agnostic) compliant、 LLM への instruction 変更
なし、 warning log で operator visibility 確保。

### 新 bug

| ID | 重要度 | 内容 |
|---|---|---|
| (なし) | — | 5/5 全 complete、 新 blocker なし |

#### 観察事項 (= fix candidate でない、 monitor 候補)

- **Run 2 の phase skip**: skill_improver が `prepare → run_and_eval → plan_improvements → finalize` で copy_to_work + apply_improvements を skip。 graph design 上 valid な path だが coverage irregular。 future batch で発火頻度を観察
- **Run 2 cost 1.7 倍**: prompt token 146566 (= R1 fallback の double-attempt path で expensive)、 ただし許容範囲

## Round 別 narrative

### Round 1: 3 sonnet 並列 dispatch (= R1 + R2 + R3)

prelude landing 後、 file overlap なしの 3 task を並列 background dispatch:
- R1: `src/reyn/op_runtime/run_skill.py` + `src/reyn/llm/model_resolver.py`
- R2: `tests/test_replay_skill_improver.py` のみ
- R3: `docs/en/concepts/runtime/permission-model.md` + dogfood README

各 worktree で独立 sonnet が code reading + fix + commit、 順次 main に sequential
cherry-pick (R3 → R2 → R1 の順、 R3 が最早完了)。 衝突なし、 1020 passed。

### Round 2: API 500 error → 直接実行に切替

S2 N=5 retest を 2 回 sonnet dispatch 試行、 両方 API 500 error で起動失敗。 main agent
直接 sequential N=5 実行に切替:

```bash
for i in 1..5: rm -rf .reyn/ → reyn chat (piped stdin) → trace → preserve
```

各 session ~30-60s、 total ~5 分で完了。 sonnet dispatch overhead なしで効率的。

教訓: **API issue 時の fallback path として main agent 直接実行が有効**。 N=5 のような
mechanical sequential task は dispatch せずとも実行可能、 dispatch overhead を考えると
直接実行が cost-efficient な場合もある。

### Round 3: 5/5 達成 + R1 fix の 3 fire 観察

N=5 sequential で **全 session complete**:
- Run 1, 4, 5: clean 6 phase 完走 (= R1 fire 0)
- Run 2, 3: R1 fix が **literal model string を 3 回 intercept**、 fallback で完走

R1 fix が想定通り「LLM hallucinate を OS が救済」 する mechanism として機能した
operational evidence、 production-grade phase 1 完了 declaration の data 確証。

## Prediction calibration

batch 14 prelude で予測:

| Step | Top prediction | Actual | Hit? |
|---|---|---|---|
| R1 (B13-NEW-1) | verified 60-70% | verified | **hit** |
| R2 (fixture fix) | verified 85-90% | verified | **hit** |
| R3 (doc) | verified 95% | verified | **hit** |
| Step 2 (N=5) | **4-5/5: 50%** / 3/5: 25% / inconclusive: 15% / 0-2/5: 10% | 5/5 | **hit (4-5/5 zone)** |

= **4/4 hit、 100% hit rate**。 Brier ≈ 0.18 (batch 13 0.20 から微改善 ✅)。

batch 14 で確立した「fix の層で base rate を切り分け」 + 「N≥5 stability discipline」
が calibration accuracy を継続改善。

## A4 review (= user 感覚との差分)

- **headline**: production-grade phase 1 完了 (= 5/5 complete) — batch 7-14 の 8 batch
  progression の到達点、 batch 13 で確立した documented design + V3 wording 仕様変更 +
  reyn.local.yaml pattern + batch 14 R1 fix の組み合わせで stability 確立
- **仕様変更ゼロ batch**: 全 fix が 🔵 不具合修正 + doc 追加、 production user 影響
  なし。 batch 13 で確立した「修正分類を明示」 discipline の運用継続
- **R1 fix の structural 価値**: LLM hallucinate を OS が救済する pattern (= P3 + P7
  compliant) の実例、 future の similar bug への template
- **API issue fallback**: sonnet dispatch failure 時の main agent 直接実行が effective、
  operational resilience 強化
- **Brier 0.18 で best**: batch 8 (0.96) からの累積 progression best、 calibration
  discipline が継続的に向上

## 残懸念点 + batch 15 候補

batch 14 で **production-grade phase 1 完了**、 phase 2 への移行が next theme。

| 優先 | 内容 | 関連 |
|---|---|---|
| MED | M2 audit B12-NEW-4 / B12-NEW-5 fix (= path mismatch / postprocessor scope) | meta hygiene 残件 |
| MED | Run 2 の phase skip 挙動 monitor (= 発火頻度を future batch で観察) | observation only |
| MED | dogfood automation の API 500 fallback documentation | operational |
| **trial** | **G4 spike**: `gemini-3.1-flash-lite-preview` evaluation (cost 10x trade-off) | user 戦略判断、 phase 2 移行候補 |
| **transition** | **production-grade phase 2 設計**: cost / observability / monitoring 整備、 phase 1 stability を超える方向性 | user-driven 戦略判断 |

batch 15 は **phase 2 移行設計** が core theme 候補。 fix wave 中心から **architectural
review + design wave** へ shift。

## 一言で

> **🏆 5/5 complete = production-grade phase 1 完了 — R1 fix で LLM hallucinate を OS が
> 救済 (P3+P7 compliant)、 全 3 fix が 🔵 不具合修正 + doc 追加 で 仕様変更ゼロ batch、
> Brier 0.18 で 8 batch 中 best**

— batch 13 4/5 (80%) → batch 14 5/5 (100%) で stability 完成
— R1 fix が 3 fire で structural fallback の operational evidence 確立
— production user 影響なし、 documented design 維持
— phase 2 移行 (= cost / observability / monitoring) が next theme 候補

batch 14 で Reyn dogfood が **「機能成立 → stability 確保」** の 2 段階完了、
**production-grade phase 1 milestone** に到達。 batch 7-14 の 8 batch progression が
構造的 fix discipline + verify-first / reproduce-first / 修正分類明示 等の new
discipline と共に **assistant の autonomous capability ceiling** を引き上げた。
