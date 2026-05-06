# B14 Step 2 — N=5 stability retest (95%+ target)

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| main HEAD | `a10553c` |
| Fixes active | R1 (B13-NEW-1) + R2 (fixture) + R3 (doc) + all earlier batch fixes |
| Setup | reyn.local.yaml temp pre-approval (= dogfood-only, reverted post-run) |
| Sample size | N=5 |
| **Complete rate** | **5/5 (100%)** ✅ |
| **Verdict** | **production-grade phase 1 完了** declaration |

## reyn.local.yaml setup (= temporary, reverted)

```yaml
permissions:
  file:
    read: allow
  python:
    pure: allow
    trusted: allow
```

= documented layer 3 mechanism (operator-personal pre-approval)、 dogfood 自動化 only。
post-run に `git restore reyn.local.yaml` で削除済。

## Per-session verdicts

| Session | Verdict | Phases | Cost | R1 fire? |
|---|---|---|---|---|
| 1 | **complete** ✅ | prepare → copy_to_work → run_and_eval → plan_improvements → apply_improvements → finalize | $0.0112 | 0 fire |
| 2 | **complete** ✅ | prepare → run_and_eval → plan_improvements → finalize (= LLM が copy_to_work + apply_improvements skip だが narrator まで完走) | $0.0190 | 2 fire (`gpt-3.5-turbo`) |
| 3 | **complete** ✅ | 6 phase clean 完走 | $0.0109 | 1 fire (`gemini-2.5-flash-lite` literal) |
| 4 | **complete** ✅ | 6 phase clean 完走 | $0.0102 | 0 fire |
| 5 | **complete** ✅ | 6 phase clean 完走 | $0.0109 | 0 fire |

= **5/5 sessions が finalize + narrator まで到達**、 user に improvement plan delivered。

## Aggregated metrics

| Metric | batch 13 | batch 14 N=5 | Delta |
|---|---|---|---|
| **Complete rate** | 4/5 (80%) | **5/5 (100%)** | **+20pp** ✅ |
| Routing-fail rate | 0/5 (0%) | 0/5 (0%) | 0pp (= V3 wording 維持) |
| R1 fix fire (= literal model fallback) | n/a | **3 fire 観察** (Run 2 × 2、 Run 3 × 1) | 全 fire で正常 fallback |
| Total cost | ~$0.053 | $0.062 | +17% (= Run 2 が double-attempt 経路で多め) |

## Delta vs batch 13 (4/5)

batch 13 で 1 partial だった原因 (= B13-NEW-1: literal `gpt-3.5-turbo` rejection)
が R1 fix で解消:

- Run 2 で R1 fix が **2 回 fire**: `op.model='openai/gpt-3.5-turbo'` を検知 → `standard` に
  fallback → LLM call 成功
- Run 3 で R1 fix が **1 回 fire**: `op.model='openai/gemini-2.5-flash-lite'` (= literal、
  proxy が直接受理する model だが model class でない) を検知 → `standard` に fallback → 成功
- 残り 3 sessions (Run 1, 4, 5) は LLM が `op.model` を出さず正常 path

= **R1 fix が真に effective + 想定通り chain 完走を救済**。

## R1 fix verification

R1 fix (= `run_skill.py` で `ModelResolver.is_known_class(name)` check) の動作確認:

```
Run 2 stderr:
  run_skill: op.model 'openai/gpt-3.5-turbo' is not a known model class —
    ignoring and inheriting runtime model 'standard' instead.
    Use a model class (light / standard / strong) defined in reyn.yaml models:.
  run_skill: op.model 'openai/gpt-3.5-turbo' is not a known model class — ...

Run 3 stderr:
  run_skill: op.model 'openai/gemini-2.5-flash-lite' is not a known model class — ...
```

= **OS boundary で fallback、 P3 + P7 compliant**:
- LLM hallucinate の literal model string を OS 側で intercept
- Skill-specific string なし (= P7)
- LLM への instruction 変更なし (= P3)
- warning log で operator に visible (= observability 維持)

verdict: **真に verified** ✅

## 新 bug

| ID | 重要度 | 内容 |
|---|---|---|
| (なし) | — | N=5 全 complete、 新 blocker なし |

ただし観察事項:
- **Run 2 の phase skip 挙動**: skill_improver が `prepare → run_and_eval → plan_improvements → finalize` で copy_to_work + apply_improvements を skip。 chain 完走したが phase coverage が irregular。 LLM が phase graph に沿わずに skip した可能性、 もしくは graph design 上 valid な path。 **fix candidate でないが monitor 候補** として記録 (= future batch で発火頻度を観察)
- **Run 2 cost 1.7 倍**: prompt token 146566 (= Run 1 の 87265 比 1.68 倍)。 R1 fix の 2 回 fire + double-attempt path 経由のため。 single-attempt path より expensive、 ただし許容範囲

## 真の milestone declaration

🏆 **production-grade phase 1 (= 機能成立 + stability) 完了**:

- chain 完走 rate: **100%** (= 5/5)
- routing reliability: **100%** (= V3 wording 維持)
- R1 model fallback: 真に effective、 LLM hallucinate を OS が救済
- documented design 整合性: 完全 (= batch 13 で revert 済)
- production user 影響: 0 (= dogfood pre-approval は operator-local)

batch 7-14 の 8 batch progression で:
- batch 7: 観測 infra 整備
- batch 8: 累積 fix verify
- batch 9: wrong layer trap 発見
- batch 10: provisional milestone (= N=1)
- batch 11: 80% routing-fail blocker 解消
- batch 12: B11-NEW-1 fix
- batch 13: doc 違反 fix revert + V3 wording + real milestone (= 4/5)
- **batch 14: B13-NEW-1 fix + 5/5 complete = production-grade phase 1 完了**

= Reyn dogfood が **「機能成立 → stability 確保」** の第 1+2 段階を data 化、 phase 2
(= cost / observability / production hardening) への移行点。
