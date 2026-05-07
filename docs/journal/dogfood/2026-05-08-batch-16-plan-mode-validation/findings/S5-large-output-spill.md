# S5: Large Output Spill (ADR-0024) — Batch 16 Finding

| Field | Value |
|---|---|
| Date | 2026-05-08 |
| Scenario | S5 — 32 KB+ step result spill-to-file |
| Agent | `b16_s5` |
| main HEAD | `4912457` |
| Sample size | N=5 |
| LLM | `openai/gemini-2.5-flash-lite` (via LiteLLM proxy, localhost:4000) |
| Driver | `/tmp/batch16/run_s5.py` |
| Findings JSON | `/tmp/batch16/S5_findings.json` |
| **Overall verdict** | **5/5 refuted** |

---

## 1. Prompt used

```
src/reyn/ 以下の全 Python ファイルを列挙し、各ファイルについて
クラス名・主要メソッド・役割を 2-3 文で説明して
```

Intended to trigger a multi-step plan (enumerate → per-file analysis → synthesis)
with step output large enough to cross the 32 KB ADR-0024 spill threshold.

---

## 2. Per-run observations

| Run | verdict | plan_invoked | spill_files | max_spill_size | reply_len (bytes) | elapsed (s) |
|---|---|---|---|---|---|---|
| 1 | refuted | False | 0 | 0 | 101 | 1.5 |
| 2 | refuted | False | 0 | 0 | 4,215 | 7.3 |
| 3 | refuted | False | 0 | 0 | 4,215 | 6.9 |
| 4 | refuted | False | 0 | 0 | 4,215 | 3.4 |
| 5 | refuted | False | 0 | 0 | 4,215 | 3.6 |

- Run 1: LLM returned an empty / error reply (101 bytes = "The model returned an empty response.").
- Runs 2–5: LLM responded with a direct-reply enumeration of exactly 10 top-level
  files in `src/reyn/` (the immediate module-level files only, not subdirectories).
  Each reply was identical at 4,215 bytes.

Plan events: zero across all 5 runs (`dogfood_trace plan-summary` → "no plan events found").

---

## 3. Two distinct failure modes

### 3a. Plan tool not invoked (primary failure — all 5 runs)

The router LLM answered the prompt directly without invoking the `plan` tool.
This is the same text-reply attractor observed in prior batch 1–14 scenarios (G1/G23),
now manifesting for a new prompt category. The prelude's R1 risk ("router LLM が
`plan` tool を invoke しない") materialised at 100% rate.

No `plan_created` event was emitted. Therefore:
- ADR-0024 spill logic was never reached.
- The `step_results/` directory under any plan workspace was never created.
- The verdict is **refuted: ADR-0024 not exercised** (not "not working").

### 3b. LLM verbosity well below 32 KB threshold (secondary finding)

Even when the LLM answered directly, it enumerated only 10 top-level files
(`src/reyn/*.py`) and produced 4,215 bytes of output — approximately 13% of the
32,768-byte spill threshold. The actual `src/reyn/` tree contains **186 Python files**
across subdirectories (`llm/`, `skill/`, `chat/`, `memory/`, `op_runtime/`, etc.).

This means that even if the plan tool had been invoked, a single step covering all
186 files at the LLM's observed verbosity (~420 bytes/file) would produce approximately
78 KB — which would exceed the threshold and should trigger spill. However, the LLM's
direct reply only covered the 10 immediate module-level files, suggesting it does not
enumerate subdirectory contents by default without explicit filesystem tool use.

**Consequence**: spill could only realistically trigger if:
1. Plan tool is invoked, AND
2. A plan step explicitly uses `list_files` or `file_read` tools to traverse all
   subdirectories, producing a large aggregated result.

Neither condition was met.

---

## 4. ADR-0024 implementation health (separate verification)

ADR-0024 was not exercised by these runs. However, the Tier 2 test suite (commit
`80e4977`) covers the spill path with synthetic payloads that exceed the 32,768-byte
threshold. The implementation is present and test-validated; this S5 run is a
**coverage gap** (= no real-LLM path through the spill code), not a code defect.

To confirm: the spill code lives at `src/reyn/plan/plan_registry.py`
(`record_step_completed` and `_step_result_path`). The `step_results/` directory
structure per ADR-0024 §2 would appear at:

```
.reyn/agents/<name>/state/plans/<plan_id>/step_results/<step_id>.txt
```

No such directories were created in any of the 5 runs.

---

## 5. Verdict calibration

Prelude S5 prediction:

| verdict | predicted | observed | delta |
|---|---|---|---|
| verified | 55% | 0% (0/5) | −55pp |
| inconclusive | 20% | 0% (0/5) | −20pp |
| refuted | 15% | 100% (5/5) | +85pp |
| blocked | 10% | 0% (0/5) | −10pp |

Brier score contribution (refuted observed, refuted predicted 0.15):
`(0 − 0.55)² + (0 − 0.20)² + (1 − 0.15)² + (0 − 0.10)² = 0.3025 + 0.04 + 0.7225 + 0.01 = 1.075`

The prediction significantly underestimated the refuted rate. The prelude's own R1
risk note ("refuted 30% は保守的だが現実的") should have been applied to S5 as well;
instead S5 carried a 15% refuted prior, which proved far too optimistic.

Root cause of miscalibration: S5 was designed assuming that a "large file enumeration
request" would reliably trigger plan tool use. In practice the same text-reply attractor
observed across all earlier scenarios applies here too — the prompt wording alone is
insufficient to force plan invocation.

---

## 6. Action items

| Priority | Item | Tag |
|---|---|---|
| HIGH | Design a prompt that forces plan tool use (e.g., explicitly names plan steps: "ステップ 1: ファイル一覧, ステップ 2: 各ファイル分析, ステップ 3: まとめ") and re-run S5 with N=5 | R1-followup |
| HIGH | Alternatively, instrument a scenario that uses a file > 32 KB as plan step input (= controlled spill trigger), rather than relying on LLM verbosity | S5-alt-design |
| MED | Add a calibration note to the prelude template: refuted prior for any "plan-dependent" scenario should be at least 40% given observed R1 base rate across S1–S5 | calibration |
| LOW | Record R1 (plan tool not invoked) base rate across all batch 16 scenarios in the retrospective for Brier score recalibration | retrospective |

---

## 7. Cross-references

- Prelude R1 risk: `../prelude.md` §8
- ADR-0024 spill design: `docs/en/decisions/0024-plan-step-result-spill.md`
- ADR-0024 implementation commit: `80e4977`
- Tier 2 spill tests: `tests/plan/test_plan_registry_spill.py` (search for `record_step_completed`)
- S1 finding (same R1 pattern): `S1-multi-source-synthesis.md` (if available)
- Findings JSON: `/tmp/batch16/S5_findings.json`
- Run log: `/tmp/batch16/S5_run.log`
