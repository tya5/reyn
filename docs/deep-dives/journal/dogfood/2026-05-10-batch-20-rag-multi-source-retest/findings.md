# Batch 20 — RAG Multi-Source Retest Findings (Aggregate)

> 1 scenario × N=3 = 3 primary runs、 main HEAD `747995c`。 S6 を batch 19
> self-audit 後に redesign (= synthetic sources で affordance conflict 排除)。
> **Pre-retrospective discipline (= 原則 batch 19 lift) を私自身も実行**:
> LLM trace dump 全 run 読み + recall ToolDefinition description 再確認 +
> scenario design 妥当性 audit。 結果、 **scenario design に 2 度目の限界が
> 判明** — affordance-bias hypothesis は依然 **pending**、 valid 判定には
> 「structurally multi-source-requiring prompt」 への 3 度目 redesign が必要。

## 1. Per-Scenario Summary

| Scenario | 予測 verified | 実測 verified | Verdict 5-tuple | Brier (5-class) |
|---|---|---|---|---|
| S6 redesign (= synthetic quantum sources) | 40% | 0% (0/3) | 0/3/0/0/0 | (= predicted/actual full breakdown 下記) |

| Verdict | 予測 | 実測 |
|---|---|---|
| verified (= multi-source picks) | 40% | 0/3 = 0% |
| refuted_b_a1 (= 1 source only) | 55% | **3/3 = 100%** |
| refuted_b_a2 (= recall non-invoke) | 5% | 0/3 = 0% |
| inconclusive | 0% | 0/3 |
| blocked | 0% | 0/3 |

**Brier (5-class)**: ((0.40−0)² + (0.55−1)² + (0.05−0)² + 0² + 0²) / 5 = (0.16 + 0.2025 + 0.0025) / 5 = **0.0730** (= 5-class 平均)

**Aggregate**:
- structural pre-check: ✓ (= recall in catalog 3/3、 2 sources visible in SP 3/3)
- behavioral: recall invoke 100%、 ただし sources field 全 run `["quantum_concepts"]` only

## 2. Pre-retrospective Discipline 実行ログ (= 原則 batch 19 lift)

retrospective 執筆前に以下 3 step を実行:

### Step 1. LLM trace dump 全 run 読み

`/tmp/reyn_s6_b20/run_{1,2,3}.jsonl` を python で parse、 全 run **完全に同 pattern**:

```
Turn 1: LLM → recall(query="...quantum bridge protocol...", sources=["quantum_concepts"])
        Tool result: 6 concept chunks returned
Turn 2: LLM → text reply (200 char、 concept-level prose)
        finish_reason=stop、 0 follow-up tool calls
```

Reply text を確認: protocol overview / handshake / decoherence buffer / use cases を network message bus 概念込みで説明、 **class 名 (= `Entangler` / `DecoherenceBuffer` / `bridge_handshake`) は不出現** (= concept doc 由来のみ、 code element 由来なし)。

### Step 2. `recall` ToolDefinition description 再確認

```
"Search indexed sources by natural-language query. Returns top-K relevant
chunks with text + metadata. Pick sources from the 'Indexed sources'
section in the system prompt."
```

→ 「pick sources」 (= 複数形) は明示、 ただし **「multi-source 推奨 / 必要なら全部 picks」 等の active guidance は不在**。 この description は LLM に sources field の cardinality を強制しない。

### Step 3. Scenario design 妥当性 audit

prompt 「How does the quantum bridge protocol work?」 が **両 source を structurally 必要とするか** を audit:

| Question | 答え |
|---|---|
| 「How does X work?」 = 純粋 concept 質問? | **Yes** — 概念説明で十分、 実装詳細は副次的 |
| Concept doc 単独で satisfactory な answer 構築可能? | **Yes** — handshake / decoherence buffer / use cases 全部 concept doc に記載 |
| Code source を読まないと答えられない aspect は? | **無し** — 実装詳細 (= class 名 / signature 等) は user query に含まれていない |
| 人間 researcher が同 query で同 routing するか? | **Yes** — concept doc 1 つで答えれば十分、 code 読む必要なし |

**結論**: scenario design は依然 **flawed**。 batch 19 self-audit で 「reyn_src_read description との textbook match」 を排除したが、 **prompt が concept-leaning** という別の confound が残った。

## 3. Affordance-bias Hypothesis Verdict (= 原則 13 Class B status update)

| Evidence path | Status | 理由 |
|---|---|---|
| Batch 18 S6 (= reyn_docs/reyn_src + 「How is recall implemented?」) | ❌ invalid | reyn_src_read description との textbook match |
| Batch 19 S6 same prompt (= post 一連 fix wave) | ❌ invalid | 同上 |
| **Batch 20 S6 redesign (= quantum_concepts/quantum_code + 「How does X work?」)** | ⚠️ **partial、 confounded** | prompt 自体が concept-leaning、 rational routing と attractor が区別不能 |

3 batches 連続で valid evidence が取れていない。 hypothesis は 「Class B = affordance-bias attractor は存在しうる」 という仮説のまま、 **decisive 判定には 4 度目の scenario re-design** が必要:

### 必要 prompt の structural property

「両 source を structurally 必要とする」 = **片方の source 単独では物理的に答えられない aspect が prompt に含まれる**。

具体的候補:

```
"Give me (a) the conceptual overview of QBP AND (b) the actual class names
 I'll need to import for integration."
```

- (a) = concept doc only (= 概念説明)
- (b) = code only (= class 名は code chunk にのみ存在: `Entangler`、 `DecoherenceBuffer`、 `BridgeState`)
- 1 source だけでは (a) or (b) のどちらかが欠ける → multi-source picks が **rational requirement**

batch 21 で同 setup (= synthetic quantum sources) + 上記 prompt で N=3 retest すると、

- verified ≥ 50% → affordance-bias hypothesis **棄却** (= LLM は structural requirement で multi-source picks する)
- verified < 30% → hypothesis **支持** (= LLM が structural requirement あっても 1 source で satisfied する真の attractor)
- 30-50% → 追加 N 必要

## 4. Calibration delta

| 軸 | 予測 | 実測 |
|---|---|---|
| structural pre-check | ✓ | ✓ |
| recall invoke rate | ~95% | 100% |
| multi-source picks rate (given recall) | 30-70% (中央 50%) | **0% (= 全 run 1 source only)** |
| **verified prediction** | **40%** | **0%** |

**真の miss は scenario design の confound**: prediction logic (= 0.95 × 0.45 ≈ 40%) は behavioral assumption が「multi-source は LLM の choice」 だったが、 実際は 「prompt が structurally 1 source で十分」 なので **measurement target 自体が不適切**。 「LLM behavior estimate の miss」 ではなく 「scenario assumption の miss」。

batch 19 self-audit で 「scenario と data の semantic match」 を audit したのに、 **prompt と prompt-required-source-count の match** という追加 dimension を audit し損ねた。 pre-retrospective discipline を **scenario design phase にも前倒し** すべきだった (= prelude 執筆時に「prompt が structurally 何 source 必要か」 を明文化)。

## 5. Carry-over

| Item | Status | 工数 | 着手 trigger |
|---|---|---|---|
| **B20-S6-1** (NEW) — structurally-multi-source-requiring prompt redesign | open、 仕様確定済 | ~0.1 day prep + N=3 retest = ~0.5 day total | user 判断、 affordance-bias hypothesis decisive 判定が必要なら batch 21 で |
| **Pre-retrospective discipline 拡張** — prelude 執筆時に「prompt が structurally 何 source 必要か」 dimension を明文化 | landed (= 本 retrospective で operationalize) | 0 | — |
| Class B status in `feedback_attractor_class_taxonomy.md` | 「pending、 evidence base 不在、 valid retest path 仕様確定」 で update | 即時 | 本 commit |

## 6. Verdict (= 1.0 release scope)

| 軸 | 判定 |
|---|---|
| 1.0 release blocker | **❌ なし** (= S5 headline + structural foundation + S9 cognitive-bias fix template で release-ready state 維持) |
| Affordance-bias hypothesis decisive 判定 | **未達**、 batch 21 候補 (= user 投資判断) |
| Pre-retrospective discipline の rigour 強化 | ✅ 実証 (= 私自身が batch 20 で適用、 結果 「2 度目 scenario flaw」 を retrospective 執筆前に発見) |

batch 20 の真の価値は:

1. **「scenario design の confound は 1 batch 1 fix では消えない」** という empirical observation
2. **Pre-retrospective discipline を main agent (= 私) が実行した first instance**、 batch 19 self-audit lesson の operational lift が functional
3. **Affordance-bias hypothesis の decisive 判定 path** (= structurally-multi-source-requiring prompt) を仕様確定、 batch 21 で実行可能 state に

「production grade landed」 narrative は batch 18 で release-blocker 解消、 batch 19 で cognitive-bias fix template + pre-retrospective discipline、 batch 20 で **scenario design rigour の継続的 audit が agent self-discipline の core practice** であることを実証。 affordance-bias 自体の decisive 判定は 1.0 release に対し **non-blocking、 投機的 follow-up scope**。
