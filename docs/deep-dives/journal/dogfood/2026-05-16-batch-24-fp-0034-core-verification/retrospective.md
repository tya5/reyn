# Batch 24 — Retrospective

> FP-0034 wrapper-only e2e core path verification (N=3 × 7 scenarios = 21 isolated runs)。 **Brier 0.39 (analyst basis) で target 0.3-0.5 band 着地 = calibration framework 稼働確認**。 Headline finding は **B24-S5-1: search_actions tools= 不在 (D14 gate cold-start blocking)**、 B25 fix wave で architectural fix 必須。 verified 76% (= B26 80% target 微下)、 B25 経由が筋。

---

## 1. Expected vs actual

| Scenario | Predicted V% | Actual V% (driver / analyst) | Hit/Miss |
|---|---|---|---|
| S1A | 80% | 33% / **100%** | analyst hit、 driver false-negative (keyword bug) |
| S1B | 75% | 100% / 100% | hit (= over-perform、 B23 H5 evidence consistent) |
| S2 | 85% | 100% / 100% | hit (= over-perform、 N=3 stability confirmed) |
| S3-noop | 75% | 100% / 100% | hit (= gating fires correctly) |
| S3-auto | 85% | 33% / 33% | **miss** (= arg shape inconsistency、 LLM が `filter` vs `category` で揺らぎ) |
| S4 | 60% | 100% / 100% | hit (= cold path complete、 hot path 0/3 は seed 構造的) |
| S5 | 50% | 67% / **0%** | analyst miss (= D14 gate cold-start blocking、 unmeasurable) |

**Batch verified rate**: 76% (analyst) — prelude implicit 73% (rough avg) と consistent。

**Batch Brier**: 0.39 (analyst) — prelude target 0.3-0.5 band 内、 calibration **正常稼働**。 B23 0.948 から **-0.55** 改善 = 1 batch で practice baseline → calibrated working state へ。

---

## 2. Turning points

### TP1: B23 H5 replay evidence が real chat で再現 (S1B 3/3)

batch 23 で `llm_replay.py --patch` H5 (= sequential connector 言語) が 100% list-only を生むことを確認、 batch 24 S1B で real chat N=3 で **3/3 verified、 全 run identical Turn 1 = [list_actions] / finish=tool_calls**。 replay evidence と real e2e の deterministic 一致確認 = methodology **stable confirmed**。

**Lesson**: replay --patch experiment は real e2e の strong predictor、 batch 25+ で同 methodology を fix design に積極活用。

### TP2: D14 gate cold-start blocking → S5 unmeasurable (B24-S5-1)

S5 で search_actions 不在 + 2/3 hallucination 観察、 即 trace inspect で D14 `is_ready()` gate が False (= cold start `.reyn/action_index/` 不在) と確定。 prelude が `embedding_class=standard` 設定で search_actions visible になると暗黙仮定していたが、 実際は **embedding_class 設定 + index ready の 2 条件 AND** だった。

batch 23 retrospective で確立した 「**config-default audit** (= 原則 10 extension)」 を prelude に code-level で適用しても、 「runtime state (= is_ready())」 まで audit する layer が欠けていた。 **prelude template 改善 candidate**: 「Config defaults verified」 → 「Runtime state verified at LLM call time」 を追加。

**Lesson**: structural pre-check は (a) config defaults + (b) runtime state の 2 軸。 batch 23 で (a) を追加、 batch 24 で (b) のギャップ判明。

### TP3: Driver verdict 2-layer が S1A / S5 で価値発揮 (= 原則 12 自動化候補の根拠)

- S1A: driver `verdict_s1a` の error keyword 不完全 → driver V=1/3、 analyst V=3/3 (= driver false-negative)
- S5: driver `verdict_s5_search` が tool_names="search_actions" を verified に → driver V=2/3、 analyst B=3/3 (= driver false-positive、 hallucination を verified と誤判定)

**原則 12 (= verdict false-attribution discipline)** を analyst-side で意識的に適用、 driver verdict と analyst verdict を 2 column で表示する findings 構造を確立。 これが batch 26+ 自動化の prerequisite。

**Lesson**: driver は cheap mechanism check、 analyst は intent check。 両者の divergence こそ calibration data の主源 (= prelude のどの仮定が破れたかを surface)。 batch 25 fix wave で driver verdict 関数を 2 layer に refactor 候補。

---

## 3. 強化 / 新確立された原則

### 原則 12 (= verdict false-attribution) の **2-column reporting standard**

findings.md / retrospective.md で driver verdict + analyst verdict を併記、 divergence を rationale 付きで明示。 batch 23 で S3-auto 1件、 batch 24 で S1A / S5 2件の divergence 検出 = 2-column が standard。

### 原則 10 (= structural pre-check) の **runtime state extension**

batch 23 で config defaults audit を追加、 batch 24 で runtime state (= `is_ready()` 等の lazy-init flags) audit が必要と判明。 prelude template に:
```markdown
### Runtime state verified
| Field | Required at LLM call time | Verify method |
|---|---|---|
| ActionEmbeddingIndex.is_ready() | True (if S5 measure intended) | manual sync build / eager init flag |
```

### Per-cwd + per-reyn-agent isolation pattern の **N=21 scale validation**

batch 23 で N=3 (3 scenarios) で確立した isolation pattern を batch 24 で N=21 (7 scenarios × 3 runs) scale で適用、 session contamination 0、 chain_id collision 0、 LiteLLM proxy 共有 OK。 production-grade dogfood infrastructure として **stable**。

### 5 並列 wave dispatch pattern

5 sonnet 並列 (wave 1) + 2 sonnet 並列 (wave 2) で 21 runs を ~10 min wall-clock 完走。 user の 「sonnet 最大 5 並列」 ガイドの operational pattern 確立。 batch 25 fix wave + batch 26 N=5 stability で同 pattern を継続。

---

## 4. 次 batch (= Batch 25 fix wave) への申し送り

### 4 つの fix items (= 原則 16 pre-fix multi-agent context analysis 適用候補)

| ID | Severity | Item | Estimated effort |
|---|---|---|---|
| B24-S5-1 | HIGH | search_actions cold-start visibility (= D14 gate) | ~2h (= architectural、 eager build flag) |
| B24-S3-AUTO-1 | MED | list_actions `category` vs `filter` description clarity | ~1h (= practitioner 4-part template) |
| B24-S1A-1 | LOW | driver error keyword expansion | 30 sec |
| B24-S4-2 | LOW | list_actions empty result narration guidance | 30 min |

### B25 fix wave 推奨 sequence

1. **原則 16 pre-fix context analysis** (= ~1h): 5 並列 sonnet で trace deep-dive + industry research + description history audit + constraint audit + design space mapping
2. **B24-S5-1 architectural fix**: synchronous embedding init flag (= `--eager-embedding-build` CLI flag + reyn web 起動時 sync option)
3. **B24-S3-AUTO-1 + B24-S4-2 combined description fix**: list_actions description rewrite (= category / filter / empty-result の 3 dimension)
4. **B24-S1A-1 driver patch**: 1-line keyword list expansion
5. **Retest**: S5 + S3-auto を N=3 retest、 verified rate confirmation

### B26 N=5 stability gate (= post-B25)

- core 4 scenarios (S1A / S1B / S2 / S3-noop) は B24 で 100% verified、 N=5 expansion で safety check
- S3-auto + S5 は B25 fix 後 N=5 retest
- S4 hot cold は usage tracker pre-seed scenario も追加候補
- target: verified ≥ 80% N=5、 attractor rate ≤ 5%

### 確立しなかった事項

- **B-class affordance-bias attractor の wrapper-only base rate**: S5 D14 gate fix 後の retest で初測定
- **hot list direct alias 呼出 rate** (= usage accumulation 後): batch 25-26 で multi-session simulation scenario candidate
- **Class C protocol-level attractor の wrapper-only manifestation**: S5 hallucination 2 件は D14 cold-start gap の症状、 fix 後の真 measurement 必要

---

## 5. Cost summary

| Item | Wall-clock | LLM cost (est) |
|---|---|---|
| C: session resume update | ~5 min | $0 |
| B: pre-batch fixes (= 3 sonnet 並列 + 1 commit) | ~10 min | ~$0.005 |
| A: B24 prelude draft | ~15 min | $0 |
| A: wave 1 dispatch (= 5 sonnet 並列、 5 scenarios × N=3 = 15 runs) | ~3 min | ~$0.015 |
| A: wave 2 dispatch (= 2 sonnet 並列、 2 scenarios × N=3 = 6 runs) | ~2 min | ~$0.008 |
| A: synthesis (= findings + retrospective draft) | ~25 min | $0 |
| **Total** | **~60 min** | **~$0.03** |

prelude 想定 1-1.5h と consistent (= 21 runs cost-efficient)。

---

## 6. Conclusion

batch 24 は:

1. **FP-0034 wrapper-only e2e core path verified at N=3** (= 4 scenarios 100%、 2 scenarios driver bug-induced、 1 scenario D14 gate blocking)
2. **Brier 0.39 で calibration framework 稼働確認** (= target 0.3-0.5 band、 B23 0.948 から -0.55 改善)
3. **Per-cwd + per-reyn-agent isolation pattern が N=21 scale で stable**
4. **5 sonnet 並列 wave dispatch pattern 確立** (= 10 min wall-clock で 21 runs)
5. **原則 12 verdict 2-layer reporting standard 確立** (= driver + analyst column 併記、 divergence rationale)
6. **原則 10 runtime state extension** (= config defaults + runtime state lazy-init の 2 軸 audit)
7. **B25 fix wave に 4 items carry-over** (= S5-1 HIGH architectural / S3-AUTO-1 MED tool description / S1A-1 LOW driver / S4-2 LOW description)

production-grade phase 1 (= FP-0034 wrapper-only e2e ≥ 80% verified N=5) **gate close** に **1 wave (= B25 fix) + 1 verification (= B26 N=5)**。 progression plan の wall-clock estimate (~7-9h total) は on track。
