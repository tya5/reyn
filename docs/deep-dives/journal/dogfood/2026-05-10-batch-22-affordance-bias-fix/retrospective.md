# Batch 22 — Retrospective

> Affordance-bias attractor (= Class B、 batch 21 で valid evidence 初取得) に対する schema-layer fix。 **5 並列 sonnet による context analysis** で fix 設計の前提を確定後、 multi-layer reinforcement (= SP rule + 2 tool description) を 1 commit で land、 N=3 retest で **0/3 → 3/3 = 100% recovery、 first attempt で達成**。 これは batch 18-20 の 「prompt-tweak speculation」 4 連続 fail 対比で **「fix 設計前 context analysis」 の決定的 effect** を実証、 batch 19 self-audit lesson の最大規模 operationalization。

---

## 1. Expected vs actual

| 項目 | 予測 (= prelude implicit) | 実測 |
|---|---|---|
| verified | 50-70% (= multi-layer reinforcement で楽観) | **100% (3/3)** |
| 1 commit 内で recovery | 期待 | ✅ 達成 |
| 残 attractor (= ctrl42 等) | ~10-20% | 0% (= 全 run reply quality high) |
| 1.0 release blocker clear | 期待 | ✅ |

予測超過の真因: **5 並列 sonnet による context analysis** が fix 設計の前提を完全に確定 → 推測 fix 試行ゼロ、 first attempt で intended layer に着地。 batch 18-20 で 4 attempts 失敗していた dimension が、 evidence ベース設計で 1 attempt success に縮約。

---

## 2. Turning points

### TP1: 5 並列 sonnet context analysis dispatch

user 指示 「**context 分析で attractor に対抗**」 を operationalize、 5 agents を info-gathering only (= no edits) で dispatch:

- A1: batch 21 vs batch 18 S5 trace deep-dive
- A2: industry research (= OpenAI / Anthropic / LangChain / MCP / blogs)
- A3: reyn_src_read description history audit
- A4: recall description constraint audit
- A5: schema-layer fix design space mapping (= 8 levers)

5 agents が独立に context を gather、 main agent が結果を synthesize。 過去 batch の 「sub-agent dispatch して報告書受領 → そのまま fix dispatch」 pattern と異なり、 **「analysis → synthesize → 1 commit fix」** という pipeline で勘ベースの介入を排除。

### TP2: A1 で 「真の attractor driver は SP rule」 と発見

batch 18-20 で私は 「tool description rewrite が affordance-bias fix の主軸」 と仮定していた。 A1 trace deep-dive が:

```
SP "Explaining Reyn" rule:
"When the user asks how Reyn works... call reyn_src_read('README.md') first."
```

を発見、 これが **batch 18 S5 verified 83%** と **batch 21 refuted 0%** を分けた structural difference の真因と判明:

- batch 18 S5 message: 「Search the docs」 explicit hint → 「When user says 'search'」 SP rule trigger → recall picks
- batch 21 message: 「What is X?」 → 「Explaining Reyn」 SP rule trigger → reyn_src_read picks

batch 19 で私が revert したのは別の guidance、 元 HN first-touch wave (= 2026-05-07 commit `f5c88ab`) で land した SP directive が真の driver だった。 **trace dump を読まずに 「description fix」 と決め打ちしていたら 4 度目失敗していた可能性**。

### TP3: A2 industry research が 「multi-layer reinforcement」 を valid pattern と確認

| Source | Pattern |
|---|---|
| OpenAI | 「Use the system prompt to describe when (and when not) to use each function」 ← exact match for our SP fix direction |
| Practitioner blogs | 4-part template (= what / when / when NOT / cross-reference by name) ← exact match for our description rewrites |
| Anthropic | structural-first (= namespacing) ← out-of-scope for our case (= tools already named differently) |

これが fix 設計を **(a) SP rule + (b) tool description 4-part template** に確定する evidence、 industry pattern と aligned で reproducible 設計と判断。

### TP4: First-attempt 100% recovery

3 file diff (= router_system_prompt.py + reyn_src.py description + recall.py description + 1 byte-identity test 更新) で N=3 retest:

- Q1: `recall(sources=['reyn_concepts'], query='care boundary')` → 1452 char accurate reply
- Q2: `recall(sources=['reyn_concepts'], query="Reyn's permission model")` → 895 char 3-layer explanation
- Q3: `recall(sources=['reyn_concepts'], query='plan mode')` → 1611 char decomposition explanation

**全 run で recall picks + meaningful answer extraction**。 batch 18-20 が 「scenario flaw / prompt fix / synthetic redesign / second confound」 で 4 連続 fail していた問題が **single context-analysis-driven fix で 100% recovery**。

---

## 3. 強化 / 新確立された原則

### 原則 13 (= attractor class taxonomy) の decisive validation

| Class | Pre-batch 22 status | Post-batch 22 status |
|---|---|---|
| A. Cognitive-bias (= S9) | ✅ Valid evidence (1 batch, 100% compliance with named callout) | ✅ Valid evidence 維持 |
| **B. Affordance-bias** | ⚠️ **partial validation** (= batch 21 で 0/3 観測のみ) | ✅ **Decisive validation** (= batch 21 0/3 + batch 22 fix 3/3 = causal evidence) |
| C. Protocol-level (= G12) | ✅ Valid evidence (既存) | ✅ 維持 |

memory `feedback_attractor_class_taxonomy.md` を **Class B = decisive validation + schema-layer fix template (= SP rule + 4-part description) 確立** に update 候補。

### 新原則 16 candidate (= multi-agent context analysis pre-fix)

batch 19 self-audit が確立した 「pre-retrospective discipline」 の上位 lift として、 **fix 設計の前段階で multi-agent context analysis を実行** する pattern を operationalize:

- **既存 (= batch 19)**: retrospective 執筆前に LLM trace + tool description + scenario design 前提を読む
- **新 (= batch 22)**: **fix 設計前** に multi-agent (= 5 並列推奨) で trace deep-dive + industry research + description history + constraint audit + design space mapping を gather、 **synthesize 後に 1 commit fix**

これは:
- batch 18-20 の 「prompt-tweak speculation 4 連続 fail」 → batch 22 の 「context-driven 1 fix 100% recovery」 という劇的 contrast で実証
- agent self-audit の operational 上限 (= 1 main agent の cognitive scope) を 5x parallel で拡張
- 「sober discipline」 を fix 設計 phase に前倒し

memory `feedback_pre_fix_context_analysis.md` (= 仮称) で operationalize 候補。

### Multi-layer reinforcement pattern (= practitioner-aligned)

industry research A2 で確認: tool description rewrite **だけ** では効果限定的、 SP rule + tool description + (optional) parameter description の **multi-layer reinforcement** が high-compliance を生む。 これは:

- B11-B13 の Reyn dogfood evidence (= ABSOLUTE rule SP の高 compliance) と aligned
- OpenAI 公式 guidance (= 「Use the system prompt to describe when (and when not) to use each function」) と aligned
- 以後 affordance-bias 系 fix の standard template として固定

---

## 4. Methodology の自己評価

### 良かった点

- **5 並列 sonnet info-gathering only (= no edits) dispatch**: 過去 batch の sub-agent fix dispatch と異なり、 main agent の synthesis stage を経由 → fix 決定が evidence chain で明示
- **A1 で SP rule を真因として発見**: 私が暗黙的に 「tool description fix」 と決め打ちしていた仮説を覆す factual evidence、 batch 18-20 の trap を回避
- **First-attempt 100% recovery**: 過去 4 attempts の 0% verified を 1 commit で 100% に押し上げ、 「context 分析 vs speculation」 の cost-effectiveness を実証
- **Constraint preservation** (= A3 / A4 audit から): C1 (file-read vs semantic distinction) + C2 (README navigation) + B17 vocab disambig + empty-state hint をすべて preserve、 fix が regression を生まない設計

### 改善余地

- **5 並列 sonnet の overhead が high** (= 各 agent ~1-2 min × 5 = 5-10 min wall-clock + main agent synthesis ~5 min): 投資判断が必要、 単一 attractor / 局所 fix なら 1-2 agents で十分かもしれない
- **A1 の発見** (= SP rule が真因) が batch 21 の retrospective 段階で見つかっていれば、 batch 22 を skip して batch 21 fix wave で完結できた可能性。 batch 19 self-audit でも気づけたはず → pre-retrospective discipline の 「読むべき場所」 リストに 「SP rule 全 lines」 が無かった
- **SP rule がある時点で問題と認識すべきだった**: batch 21 retrospective で 「reyn_src_read description claim」 だけに focus、 SP rule を追わなかった。 future audit で 「SP の 'When user...' rules を全 enumerate」 を pre-retrospective discipline checklist に追加候補

---

## 5. 次 batch (= optional cross-validation) への申し送り

### Cross-validation candidate (= optional、 user 投資判断)

batch 22 は N=3 で 100% verified、 evidence は十分だが **stability check** として:

| Path | 工数 | 価値 |
|---|---|---|
| N=5+ retest with same prompts | ~0.2 day | stability confirmation、 ただし batch 18 S5 拡張 N=12 で 83% verified の prior (= 100% は ceiling 近い) |
| 別 prompt class (= 「How is X implemented?」 = code-reading query) で test | ~0.3 day | C2 fallback (= reyn_src_read for source-code queries) が機能するか確認、 multi-class coverage |
| 別 indexed source topics (= memory / reyn_src 等) で test | ~0.3 day | source 毎 description quality と routing accuracy の相関測定 |

これらは **post-1.0 fast-follow scope**、 1.0 release blocker は batch 22 で clear 済。

### 1.0 release narrative draft

batch 22 完了で 「framework foundation + headline scenario green (= natural query 含む) + cognitive-bias fix template + affordance-bias fix template + dogfood discipline self-correction infra」 の core asset が完成、 **1.0 OSS launch narrative draft** が次 wave の natural candidate (= README rewrite + blog + HN draft、 ~0.5-1 day)。

---

## 6. Conclusion

batch 22 は:

1. **Affordance-bias attractor の decisive validation + fix template 確立** (= Class B、 schema-layer multi-layer reinforcement)
2. **「Context 分析で attractor に対抗」 user 指示の operational implementation** (= 5 並列 sonnet info-gathering、 batch 19 self-audit lesson の最大規模 operationalization)
3. **1 commit / first attempt / 100% recovery** = 過去 4 attempts の 0% 連続 fail 対比で context-driven fix の cost-effectiveness 実証
4. **1.0 release blocker clear** = natural concept query で recall pickups 動作確認、 1.0 launch narrative defendable state

dogfood discipline framework の進化:

- batch 17: structural pre-check 必須 (= 原則 10)
- batch 18: structural × behavioral 軸分離 + verdict false-attribution (= 原則 11 + 12)
- batch 19 (revised): cognitive-bias fix template + pre-retrospective discipline + Class B downgrade
- batch 20: scenario design audit checklist (= 原則 14)
- batch 21: prompt class taxonomy candidate (= 原則 15) + affordance-bias partial validation + real e2e first instance
- batch 22: **affordance-bias decisive validation + schema-layer fix template (= multi-layer reinforcement) + 「fix 設計前 multi-agent context analysis」 candidate (= 原則 16)**

「production grade narrative の sober discipline で再構築」 (= batch 17 retrospective 末尾宣言) は、 batch 22 で **「decisive RAG narrative + fix template 2 系統 (cognitive + affordance) + agent self-discipline 2 段 (pre-retrospective + pre-fix)」** の form で具体化された。 1.0 OSS launch narrative の core asset が完成、 **release-readiness state**。
