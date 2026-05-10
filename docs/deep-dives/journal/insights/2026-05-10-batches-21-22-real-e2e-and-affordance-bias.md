# 2026-05-10 — Batches 21 + 22: real e2e first instance + affordance-bias decisive validation + pre-fix context analysis

> 同 1 day 内 batches 17-20 で確立した dogfood discipline framework を、
> batches 21-22 が **real-world ground truth** へ拡張。 (a) real `gemini-embedding-001`
> + 21 EN concept docs での e2e first instance (= synthetic content からの脱却)、
> (b) affordance-bias attractor の **3 batches 連続 confound を valid evidence で
> 解消**、 (c) **5 並列 sonnet pre-fix context analysis** が 1 commit / first
> attempt / 100% recovery を達成。 前 insight (= batches 17-20) の延長として、
> dogfood discipline 4-tier prediction + 4-dim audit + 2-stage self-discipline
> の operational ladder が **3 stage** (= predict / pre-retrospective / pre-fix)
> に拡張された記録。

## 観測 — 2 batches で 4 つの first instance

### Batch 21 (= real e2e dogfood first instance)

batch 17-20 は synthetic content (= driver script の Python literal) + Fake
EmbeddingProvider で配線 layer を verify、 ただし **「実 user が実 doc を indexing
して chat する」 flow は未検証** だった。 batch 21 は:

- main agent (= 私、 Claude) が sub-agent dispatch なしで direct execution
- `/tmp/reyn_e2e_smoke/` workspace で main repo .reyn 不汚染
- **real `gemini-embedding-001`** via LiteLLM proxy (= dummy `OPENAI_API_KEY` で接続可)
- **real `docs/concepts/*.md` 21 EN files** を indexing (= 418 chunks、 cost ~$0.001)
- N=3 chat queries (= **natural concept questions**、 explicit-search hint なし)

結果:

1. **B21-S0-1 [HIGH] description/path propagation bug** が surface + in-flight fix:
   - `IndexWriteIROp` schema に `description` / `path` field 不在
   - `index_docs` skill postprocessor が user-input を propagate できず、 `index_write.py` handler が placeholder fallback (= 「Index of source 'X'」 / 「(unknown)」) で SourceManifest に書き込み
   - SP の 「Indexed sources」 section が placeholder 表示 → LLM が source content 評価不能 → routing 精度低下
   - batch 17 で deferred MED 扱いだったが real e2e で routing への直接影響を確認、 HIGH に re-classify + immediate fix
   - schema 拡張 + handler caller-priority resolve + skill args_from で 3 file diff、 1 commit で landing

2. **B21-S0-2 [HIGH] affordance-bias attractor の valid evidence 初取得**:
   - description fix landing 後も N=3 retest で **3/3 で `reyn_src_read` picks**
   - LLM が **存在しない path を guess** (= `docs/en/concepts/care-boundary.md` 等、 mkdocs i18n pattern による generic LLM prior)
   - SP に正しい description (= 「Reyn's design concepts and architectural principles」) が表示されているのに **recall が 0/3 で picks されず**
   - batch 18-20 で 3 batches 連続 confound に阻まれていた **Class B (= affordance-bias) hypothesis の valid evidence 初取得**

3. **新原則 15 candidate (= prompt class taxonomy) の発見**:
   - batch 18 S5 (= 83% verified) の prompt は 「**Search the docs**. What does the recall tool do?」 で **explicit-search hint** を含む
   - batch 21 の prompt は 「What is the care boundary in Reyn?」 = **natural concept question**、 search verb なし
   - 同 source / 同 model / 同 tool でも prompt class で result が drastically 異なる
   - prompt class subdivide が discipline framework に未確立だった、 future prelude で必須化

### Batch 22 (= affordance-bias schema-layer fix + pre-fix context analysis first instance)

user 指示 「**context 分析で attractor に対抗、 sonnet 最大 5 並列活用**」 が
operational pattern を駆動:

1. **5 並列 sonnet info-gathering only dispatch** (= no edits、 read-only):
   - **A1 trace deep-dive**: batch 21 vs batch 18 S5 を比較、 LLM-input level の最小 structural difference を特定
   - **A2 industry research**: OpenAI / Anthropic / LangChain / MCP / practitioner blogs で同 affordance conflict を扱う pattern
   - **A3 reyn_src_read description history audit**: 元 commit (= `f5c88ab` 2026-05-07 HN first-touch wave) + 元 motivation (= web_search 対抗) + preserved use cases (= constraint set)
   - **A4 recall description constraint audit**: empty-state / vocab disambig / required field semantics
   - **A5 schema-layer fix design space mapping**: 8 levers を effort × evidence × risk で ranking

2. **A1 で発見**: 真の attractor driver は **SP-level rule** だった、 tool description ではなかった

```
SP "Explaining Reyn" section:
"When the user asks how Reyn works or wants to understand any part of Reyn's
 implementation, your authoritative source is Reyn's own repository — call
 reyn_src_read('README.md') first."
```

batch 18 S5 と batch 21 の structural difference は **user message 内 "Search" keyword の有無** で別 SP rule が trigger していた:

- B18 prompt: 「Search the docs」 → 「When user says 'search'」 SP rule → recall picks
- B21 prompt: 「What is X?」 → 「Explaining Reyn」 SP rule → reyn_src_read picks

私が **batch 19 で revert したのは別の guidance**、 元 HN first-touch wave で land した SP directive が真の attractor source。 trace dump を読まずに 「description fix」 と決め打ちしていたら 4 度目失敗していた可能性。

3. **A2 で確認**: industry pattern は **multi-layer reinforcement** + 4-part description template (= what / when / when NOT / cross-reference by name) + OpenAI 公式 「Use the system prompt to describe when (and when not) to use each function」 endorsement

4. **Synthesis → 1 commit fix**:
   - SP rule (= 「Explaining Reyn」) を **indexed sources 条件付き** に rewrite (= primary lever)
   - `reyn_src_read` description を 4-part template で narrow (= secondary lever、 C1+C2 constraint preserve)
   - `recall` description を concrete use case 列挙 + cross-reference で strengthen (= secondary lever)
   - 1 byte-identity test 更新 + 4 router replay fixtures re-record

5. **Same N=3 retest**:
   - Q1 「What is the care boundary?」 → `recall(['reyn_concepts'], 'care boundary')` → 1452 char meaningful reply ✅
   - Q2 「Explain Reyn's permission model.」 → `recall(['reyn_concepts'], "Reyn's permission model")` → 895 char 3-layer explanation ✅
   - Q3 「What is plan mode?」 → `recall(['reyn_concepts'], 'plan mode')` → 1611 char decomposition explanation ✅
   - **3/3 verified、 batch 21 0/3 → batch 22 3/3 = +100pp、 first attempt**

## 因果分析 — なぜ context analysis が 4 attempts 失敗を 1 attempt success に縮約したか

### 表層: pre-fix で root cause を確定 → speculation 撤廃

batch 18-20 は 「prompt-tweak speculation」 anti-pattern:
- batch 18 S6 当初 fix: prompt rule 追加 → 0/3
- batch 19 S6 retest: 同 fix landed → 0/3 (改善ゼロ)
- batch 19 self-audit: revert (= scenario flaw 判明)
- batch 20 S6 redesign: synthetic sources で affordance conflict 排除 → 0/3 (= 別 confound)
- 累積 ~4 hour、 各 attempt 局所的に rational だが系列としては speculation chain

batch 22 は 「context-driven design」:
- 5 parallel info-gathering ~10 min wall-clock
- main agent synthesis ~5 min
- 1 commit fix → 3/3 first attempt
- net cost reduction ~85% + production-grade fix

### 中層: 5x parallel cognitive scope が main agent limit を超える

main agent (= 私、 single Claude session) の audit limit は cognitive scope に
依存。 batch 19 self-audit で 「LLM trace + tool description + scenario design 前提」 を
読むと自覚していたが、 **「SP rule 全 lines を re-read」** は checklist に
含まれていなかった。 結果、 batch 21 retrospective でも SP rule を catch せず、
batch 22 で 5 parallel agents の A1 trace deep-dive が独立に発見。

これは **「agent self-discipline は単独で operational ceiling がある、 並列 cognitive scope で突破できる」** という empirical observation。

### 深層: 「sober discipline」 を 「fix 設計 phase」 に前倒し

batch 17 retrospective 末尾: 「production grade narrative の sober discipline で再構築」 宣言。 batch 19 で **retrospective 執筆 phase** に discipline 適用、 batch 22 で **fix 設計 phase** に前倒し。

3 stage agent self-discipline ladder:
1. **Prediction phase** (= prelude): predict before observing (原則 11)
2. **Retrospective phase** (= 結果 → 学び): pre-retrospective discipline (原則 batch 19)
3. **Fix design phase** (= 学び → action): pre-fix multi-agent context analysis (原則 16)

各 stage で 「observation infra 完備しても discipline で skip すると過剰一般化」 という二次的 trap が surface する経路を、 順次 catch。

## 教訓 — future contributor 向け operational summary

### Real e2e dogfood の minimum viable shape

```bash
# 1. Setup (= isolated workspace)
mkdir -p /tmp/reyn_e2e_smoke && cd /tmp/reyn_e2e_smoke
cp <repo>/reyn.local.yaml .  # for proxy api_base
OPENAI_API_KEY=dummy reyn agent new default

# 2. Index real project content
OPENAI_API_KEY=dummy LITELLM_API_BASE=http://localhost:4000 \
  reyn run index_docs '{"source": "<name>", "path": "<absolute_glob>", "description": "<semantic_description>"}'

# 3. Verify SourceManifest state
reyn source describe <name>
# → Description should match user input (= B21-S0-1 fix verification)
# → Path should match user glob (= B21-S0-1 fix verification)

# 4. N=3 natural concept queries
for q in "What is X?" "Explain Y." "How does Z work?"; do
  rm -f .reyn/agents/default/history.jsonl
  REYN_LLM_TRACE_DUMP=/tmp/q_$RANDOM.jsonl reyn chat --cui <<<"$q"
done

# 5. Verify trace dumps
# → recall should be invoked with correct sources field
# → reply should reflect indexed chunks content (= meaningful, not "couldn't find")
```

### Pre-fix multi-agent context analysis pattern

attractor 系 fix を設計する時の 5 agent fan-out:

1. Trace deep-dive (= 当該 batch の trace + 比較対象 batch の trace)
2. Industry research (= same affordance conflict を扱う external pattern)
3. Description / SP rule history audit (= 元 commit + motivation + preserved use cases)
4. Constraint audit (= empty-state / vocab / required field の preserve 必須要素)
5. Design space mapping (= fix lever 全 enumerate + ranking)

main agent synthesize → 1 commit multi-layer fix。 sub-agent は **info-gathering only** (= no edits) を厳守。

### 4-tier prediction + 4-dim audit + 3-stage self-discipline

```
Prediction (prelude):
  ├─ Structural axis (= deterministic, binary)            ← 原則 10
  ├─ Behavioral axis (= stochastic, base rate)             ← 原則 11
  │   ├─ Class A cognitive-bias                            ← 原則 13
  │   ├─ Class B affordance-bias                           ← 原則 13
  │   └─ Class C protocol-level                            ← 原則 13
  ├─ Verdict false-attribution discipline                   ← 原則 12
  └─ Brier calibration self-audit                           ← 既存

Scenario design audit (prelude):                            ← 原則 14
  ├─ Dim 1 Data semantic match
  ├─ Dim 2 Tool affordance match
  ├─ Dim 3 Structural source-count requirement
  └─ Dim 4 Rational alternative paths

Self-discipline (= 3 stages):
  ├─ Predict before observing (= 原則 11)
  ├─ Pre-retrospective discipline (= 原則 batch 19)
  └─ Pre-fix multi-agent context analysis (= 原則 16)

Prompt class taxonomy (= prelude classification):           ← 原則 15
  ├─ P-explicit (= search verb 含む、 base rate 高)
  └─ P-natural (= 自然な質問、 base rate 低)
```

## 関連

- 9 原則 framework 確立期: `docs/deep-dives/journal/dogfood/2026-05-04..2026-05-06-batch-7..14/` 各 retrospective
- batch 17-20 progression insight: `docs/deep-dives/journal/insights/2026-05-10-dogfood-discipline-evolution-batches-17-20.md`
- batch 21 retrospective: `docs/deep-dives/journal/dogfood/2026-05-10-batch-21-rag-real-e2e/retrospective.md`
- batch 22 retrospective: `docs/deep-dives/journal/dogfood/2026-05-10-batch-22-affordance-bias-fix/retrospective.md`
- 統合 dogfood-discipline doc: `docs/deep-dives/contributing/dogfood-discipline.md` (= 原則 15 + 16 含む)
- Memory: `feedback_attractor_class_taxonomy.md` (= Class B decisive validation 後) / `feedback_pre_retrospective_discipline.md` / `feedback_pre_fix_context_analysis.md` (= 原則 16 lift) / `feedback_envelope_layer_fix.md` (= 既存)
