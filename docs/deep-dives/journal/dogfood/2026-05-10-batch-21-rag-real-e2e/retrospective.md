# Batch 21 — Retrospective

> Real e2e dogfood (= 21 markdown → 419 chunks → real gemini-embedding-001 → SQLite → recall query) を main agent (= 私) が直接実行。 **Indexing 経路 ✓**、 **chat 経路で 0/3 verified + 真の affordance-bias evidence 初取得**、 **B21-S0-1 description/path propagation bug を fix wave 内で land**。 batch 17-20 が捉えられなかった real-world UX gap が surface、 **1.0 release narrative の sober re-evaluation** が必要と判明。

---

## 1. Expected vs actual

| 項目 | 予測 | 実測 |
|---|---|---|
| Indexing e2e 動作 | ✓ 想定 | ✓ 419 chunks / 418 written / ~$0.001 |
| recall invoke rate (natural concept query) | 40-60% | **0/3 = 0%** |
| 新 bug count | 0-2 | **2** (= B21-S0-1 description bug、 B21-S0-2 affordance-bias) |
| Affordance-bias hypothesis evaluation | 4 度目 attempt | **valid evidence 初取得** (= confound 排除条件下で 3/3 refuted) |

予測の真の miss: 「dim 2 ⚠️ を承認しつつ 50% verified prediction」 という楽観バイアス。 weak LLM (gemini-flash-lite) が prompt 「what is X」 / 「explain X」 と reyn_src_read description claim 「how does Reyn / how does Reyn's X work?」 の semantic 区別を持たないという base rate を予測 logic に反映していなかった。

---

## 2. Turning points

### TP1: Indexing 経路の real e2e first verification

batch 17-20 は driver-side `write_index_directly` で chunks を SQLite に直接書き込み、 `index_docs` skill (= Phase 1 strategy LLM + chunkers.py + Skill.postprocessor) は **bypass** していた。 batch 21 で **初めて真の skill 経路を通った**:

- Phase 1 strategy LLM (gemini-flash-lite) が boundary=heading + max_chunk=600 + overlap=0.1 を picks (= reasonable choice)
- chunkers.py が 21 markdown を 419 chunks に分割 (= heading-based)
- embed op handler が 419 chunks を batch で gemini-embedding-001 に送信 (= 3072-dim vectors)
- index_write op handler が SqliteIndexBackend に書き込み (= 418 written + 1 dedup)
- SourceManifest upsert (= ただし B21-S0-1 で description 消失)

skill 経路全体が **real content + real embedding** で動作することを first time confirm。 `skill.md`-driven indexing strategy という 1.0 narrative core が valid。

### TP2: B21-S0-1 description/path propagation bug の即時 fix

`reyn source describe` で「Description: Index of source 'reyn_concepts'」 (= placeholder) を観測、 root cause を `IndexWriteIROp` schema に description/path field 不在と特定、 即時 fix:

- `src/reyn/schemas/models.py`: schema 拡張
- `src/reyn/op_runtime/index_write.py`: handler caller-priority resolve
- `src/reyn/stdlib/skills/index_docs/skill.md`: postprocessor args_from 拡張

batch 17 で deferred MED として記載していた B17-S3-2 と同 bug、 real e2e で HIGH (= routing 精度に直接影響) に re-classify。 dogfood batch が classification 訂正を駆動した instance。

### TP3: Affordance-bias hypothesis の valid evidence 初取得

batch 18-20 は 3 batches 連続で scenario design flaw により Class B (= affordance-bias) hypothesis を valid evidence で評価できなかった。 batch 21 は **description fix landed + real content + real embedding + 自然な概念 prompt** という、 過去 batch の confound すべてを排除した状況で:

- N=3 全 run で `reyn_src_read` picks
- LLM が **存在しない path を guess** (= `docs/en/concepts/care-boundary.md` 等、 hallucinated)
- SP に 「Reyn's design concepts and architectural principles (418 chunks)」 と informative description あり
- それでも recall は 0/3 で picks されず

これが **batch 19 self-audit で 「hypothesis pending」 とした Class B の partial validation** に最も近い data。 ただし 1 batch / 1 prompt class のみなので 「decisive 判定」 ではなく **「hypothesis 支持の evidence 取得」** の段階。 batch 22 で schema-layer fix attempt + retest が decisive 判定となる。

### TP4: batch 18 S5 headline 達成の re-interpretation

batch 18 S5 (= headline scenario) prompt 「**Search the docs**. What does the recall tool do?」 で 3/3 primary + 拡張 N=12 で 83% verified。 当時 「production-blocker 解消」 narrative の core asset。

batch 21 で 「**Search**」 instruction 抜きの自然な概念 query では 0/3 refuted。 これは:

- batch 18 S5 の verified rate は **「Search the docs」 explicit hint に依存**していた
- 自然な user query (= 「What is X?」) では別の base rate (= ~0%)
- 1.0 release narrative の 「headline scenario green」 主張は **prompt class 限定** で valid

これは **dogfood batch progression が user-realistic prompt class へ移行する必要** を示す observational lesson。 過去 batch が 「ground truth user behavior」 を測定していなかったわけではなく、 **prompt-class taxonomy** が discipline に未確立だった。

---

## 3. 強化 / 新確立された原則

### 原則 batch 19 (= pre-retrospective discipline) の独立 self-execution

batch 20 で main agent が初実行、 batch 21 で 2 度目。 今回は scenario design audit を prelude 段階で実行 (= dim 2 ⚠️ を明示記録)、 trace dump audit を retrospective 執筆前に実行、 結果として **affordance-bias evidence の正しい解釈** + **B21-S0-1 即時 fix decision** + **1.0 narrative re-evaluation** という 3 つの正しい判断を chain で実行できた。 batch 19 self-audit lift が operational に sustainable であることを confirm。

### 原則 14 (= scenario design audit checklist) の operational 検証

batch 21 prelude は dim 2 ⚠️ を **明示的に承認** + 「これが measurement target」 と framing。 結果 actual 観測 (= 0/3) が prelude 仮説 (= dim 2 で reyn_src_read affordance pull が surface する) と整合、 過剰一般化 trap を回避。 4 dim audit checklist が batch 22 以降の prediction logic に **「dim 2 ⚠️ scenarios の verified rate base rate」** を追加 base data として供給。

### 新 candidate 原則 15 (= prompt class taxonomy)

batch 21 で surface した observation を一般化:

dogfood batch の prompt は **少なくとも 2 class** に subdivide:

- **Class P-explicit**: search hint (= 「Search the docs」 / 「look up」 / 「find」) を含む prompt。 user の意図が tool-level で明示。
- **Class P-natural**: search hint なき自然な question (= 「What is X?」 / 「How does X work?」)。 user は tool-level routing を意識しない。

両 class は **base rate が異なる**:
- Class P-explicit: tool 選好が prompt-driven、 affordance-bias は弱め
- Class P-natural: tool 選好が description / catalog-driven、 affordance-bias が surface しやすい

**Implication**: dogfood prelude は scenario の prompt class を明示、 prediction の base rate を class 依存で固定。 batch 18 S5 は P-explicit で 83% verified、 batch 21 は P-natural で 0% verified、 同じ source / 同じ tool / 同じ model でも prompt class で結果が変わる。

memory `feedback_prompt_class_taxonomy.md` (= 仮称) で operationalize 候補。

---

## 4. 次 batch (= batch 22 候補) への申し送り

### Carry-over fix queue

| Item | Severity | 工数 | 着手 trigger |
|---|---|---|---|
| **B21-S0-2 schema-layer fix** (= recall description 強化 + reyn_src_read narrowing) | HIGH | ~0.4 day | 1.0 release blocker 候補 promote、 user 判断 |
| Batch 22 retest (= 同 prompt N=3、 fix landed 後の verified rate 測定) | — | ~0.5 day | 上記 fix landed 後 |
| **B21-S0-1 unit / integration test** (= description/path round-trip via index_docs skill) | LOW | ~0.2 day | follow-up wave、 regression 防止 |
| Class P-natural の base rate measurement (= 別 prompt class で N=5+) | MED | post-1.0 | discipline operationalize |
| Class B (= affordance-bias) decisive 判定 | (依存) | batch 22 で fix attempt 結果次第 | — |

### 1.0 Release narrative re-evaluation

batch 21 結果は 1.0 OSS launch narrative に **scope adjustment** を要求:

- ✅ 維持: 「framework foundation provided」 (= indexing 経路 e2e ✓)
- ✅ 維持: 「skill.md-driven indexing strategy override」 (= chunker hot-swap pattern 機能)
- ⚠️ 訂正: 「headline scenario green」 → 「**explicit search query** で headline scenario green、 natural query は post-1.0 fast follow scope」
- ⚠️ NEW gate: B21-S0-2 schema fix を 1.0 release blocker 候補に promote、 さもないと user の自然な使い方で 「RAG 動かない」 体験。 ~0.4 day の fix で 1.0 narrative の believability が大幅向上。

### Calibration adjustments

batch 22 prelude で:

- prompt class taxonomy (= P-explicit / P-natural) を明示 row 追加
- dim 2 ⚠️ scenarios の base rate を batch 21 evidence (= 0%) で初期化
- `recall` description fix 効果は 「P-natural で base rate どれだけ shift するか」 を測定する形で frame

---

## 5. Methodology の自己評価

### 良かった点

- **main agent (= 私) が e2e dogfood を直接実行**、 sub-agent dispatch を経由せず scenario design / execution / pre-retrospective audit / fix decision / retrospective 執筆を 1 sequence で完了。 過去 batch の sub-agent vs main agent split よりも cause-effect の連結が tight、 学びが明示的
- **Pre-retrospective discipline self-execution** が batch 20 (= sub-agent dispatch) と batch 21 (= main agent direct) の両 mode で functional であることを実証
- **Bug fix decision の sober execution**: B21-S0-1 を 「dogfood で観察、 即時 fix、 retest で fix 効果と次 layer 課題分離」 の sequence で実行、 fix の効果と残存問題を separate evidence として記録
- **真の affordance-bias evidence** を 3 batches の confound を経て **valid scenario** で初取得、 batch 19 self-audit で 「hypothesis pending」 と downgrade した Class B の partial validation

### 改善余地

- **Prelude prediction の楽観バイアス継続**: dim 2 ⚠️ を承認しつつ 50% verified を予測したのは batch 18 S5 の 83% に引きずられた anchoring bias。 weak LLM の semantic distinction 限界を base rate に反映していなかった
- **Prompt class taxonomy が discipline に未確立だった**: batch 18 S5 で 「Search the docs」 hint を含む prompt が 83% verified を出した時点で、 「これは P-explicit class の base rate」 と framing すべきだった。 batch 21 で初めて class subdivide が surface
- **B21-S0-1 が batch 17 で deferred MED 扱いだったのは miss-classification**: real e2e で routing 精度に直接影響する HIGH と即判明。 fix classification rule に 「user-facing UX に display される field の bug は HIGH default」 を加える discipline 候補

---

## 6. Conclusion

batch 21 の真の価値は 3 つの first instance に同時到達:

1. **Indexing 経路 real e2e first verification** — `skill.md`-driven RAG が real content + real embedding で動作確認、 1.0 framework foundation narrative の core が valid
2. **B21-S0-1 description/path propagation bug の即時 fix landed** — real e2e でしか surface しない type の bug を dogfood で discover + fix + verify の 1 batch chain
3. **Affordance-bias hypothesis の valid evidence 初取得** — batch 18-20 の 3 batches 連続 confound を batch 21 が排除、 Class B partial validation 達成

1.0 OSS launch narrative は batch 17-20 で 「headline scenario green」 主張を確立、 batch 21 で **prompt class taxonomy 不在による 楽観 narrative** が判明、 **「explicit search query で headline green、 natural query は schema-layer fix で fast follow」** という sober scope adjustment が必要。 schema-layer fix (= ~0.4 day) を 1.0 release blocker 候補に promote する判断が user 投資の最重要 lever。

dogfood discipline framework の進化:

- batch 17: structural pre-check 必須 (= 原則 10)
- batch 18: structural × behavioral 軸分離 + verdict false-attribution (= 原則 11 + 12)
- batch 19 (revised): cognitive-bias fix template (= S9 valid) + pre-retrospective discipline + Class B downgrade
- batch 20: scenario design audit checklist (= 原則 14) + main agent self-execution
- batch 21: **prompt class taxonomy candidate (= 原則 15)** + **affordance-bias partial validation** + **fix-classification rule 強化候補** + **real e2e first instance**

「sober discipline で再構築」 という batch 17 retrospective の宣言は、 batch 21 で **「production grade narrative の prompt class limitation を honest に scope」** という形で具体化された。
