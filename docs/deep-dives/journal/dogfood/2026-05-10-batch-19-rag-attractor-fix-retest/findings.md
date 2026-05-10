# Batch 19 — RAG Attractor Fix Retest Findings (Aggregate)

> **2026-05-10 self-audit revision**: 当初 retrospective で S6 0/3 を
> 「affordance-bias attractor、 prompt-layer fix exhausted」 と結論し、
> 原則 13 (= attractor class taxonomy) を確立したが、 user 指摘で **scenario
> design flaw** が真因と判明。 LLM trace + tool description を **retrospective
> 執筆前に読んでいなかった** のが直接原因。 本 doc は revision 後の正しい
> 結論を記載、 当初版の誤解部分は §6 self-audit に保存。

> 2 scenarios × N=3 = 6 primary runs、 main HEAD `ef70aef` (= 後 `1c5856d`
> commit batch 19 docs landed → **revert/correction commit で B19 R-RAG-srcread
> guidance 撤去**)。
> **S9 が 3/3 verified で full recovery (= cognitive-bias attractor が
> named anti-attractor callout で 100% override = 真の学び)**、
> **S6 は 0/3 refuted だが scenario design flaw で attractor 観測無効
> (= prompt 「How is recall implemented?」 が code-reading query、
> indexed `reyn_docs` に answer 含まれず、 LLM が `reyn_src_read` を選んだのは
> 正しい routing)**。

## 1. Per-Scenario Summary

| Scenario | 予測 verified | 実測 verified | Verdict 4-tuple | Brier (4-class) | 結論 |
|---|---|---|---|---|---|
| **S9** cost preflight | 65% | **100%** (3/3) | 3/0/0/0 | **0.215** (vs B18 1.055) | ✅ named anti-attractor callout が cognitive-bias attractor を 100% override (= 真の学び) |
| S6 multi-source recall | 50% | 0% (0/3) | 0/3/0/0 | 0.154 (vs B18 0.264) | ⚠️ **scenario design flaw**、 attractor 観測無効。 LLM が `reyn_src_read` を選んだのは tool description に従った正しい routing |

**Aggregate**:
- primary verified (= S9 のみ valid evidence): 3/3 = 100%
- S6 は scenario invalid、 separate retest 必要 (= prompt を indexed docs の content と semantic match させる redesign 後)
- mean Brier: S9 single 0.215 (= B18 1.055 から 80% 改善)、 S9-record の数字
- structural pre-check: 2/2 = 100%

## 2. S6 Scenario Design Flaw — Detailed Analysis (= self-audit 結果)

### 観測された LLM 行動

全 3 run、 LLM の routing pattern は:

```
Turn 1: User → "How is recall implemented?"
Turn 1: LLM  → reyn_src_read(path="README.md")
Turn 2: LLM  → text reply (= recall の正確な description)
```

LLM trace で `tool_calls[0].function.name == "reyn_src_read"`、 `arguments.path == "README.md"` を 3/3 確認。 turn 2 で recall の正しい description を text 返答。 **LLM は user query に正しく回答している**、 ただし recall tool ではなく reyn_src_read tool を使った。

### Tool description signal の照合

| Signal source | Content | S6 prompt との match |
|---|---|---|
| `reyn_src_read` description | 「Use this for any 'how does Reyn / how does Reyn's X work?' question — Reyn's source is the authoritative answer」 + 「Start with reyn_src_read('README.md') for an overview」 | **完全 textbook match** |
| `recall` description | 「Search indexed sources by natural-language query. Returns top-K relevant chunks with text + metadata.」 | generic、 「implementation」 query への explicit signal なし |
| `reyn_docs` indexed content | concept documentation (= `docs/concepts/*.md`) | **implementation 詳細含まない** |

LLM が **specialised tool description** を **generic SP guidance** より優先したのは合理的判断。

### 真の問題 = scenario design

「How is X **implemented**?」 = code-reading query。 expected answer は **source file** に存在、 concept doc には存在しない。 S6 の expected verdict (= 「recall を invoke で sources field に reyn_docs + reyn_src 両方含む」) は **prompt と data source の semantic match を保証していない設計ミス**。

### batch 18 の S6 結論も同 flaw

batch 18 retrospective で 「R-RAG-srcread = LLM が reyn_src_read を recall より選好する affordance-bias attractor」 と命名・記載したが、 同 scenario design で測定した結果なので **batch 18 結論も同様に retrospective revision 候補**。 batch 18 の同 prompt 「How is recall implemented?」 で LLM が同 reasoning を行った可能性が high。

## 3. S9 Cost Preflight Gate — 真の学び

S6 の scenario flaw に対し、 S9 の result は **valid evidence**:

### 観測

- batch 18: 0/3 verified (= LLM が `threshold_exceeded:true` boolean flag を ignore して `$0.0003` numeric に anchor、 abort せず)
- B19 fix: `phases/strategy.md` に **named anti-attractor callout** 追加 — 「Common attractor to avoid: when boolean flag says X but dollar value is small, do NOT conclude Y. Boolean policy flag wins over numeric estimate.」
- batch 19: 3/3 verified (= 100% recovery)
- **Run 2 smoking gun**: LLM が `control.reason.summary` で `$0.0003 USD` を自己引用しつつ `control.type: "abort"` を出力 — callout が **active override** として機能した証拠

### 学び (= valid)

**Cognitive-bias attractor (= LLM が input data を見ているが evidence の比重を間違える pattern)** に対しては、 **named anti-attractor callout** (= 「Common attractor to avoid: when X, do NOT Y. Z wins over W.」 形式) が prompt-layer で effective。

batch 6-12 で確立した「individual MUST bullet rule」 が 60-80% compliance だったのに対し、 named callout pattern は **100% compliance** を達成。 これは prompt-layer fix template として transferable。

## 4. 原則 13 — Downgraded

当初 batch 19 で「Class A cognitive-bias / Class B affordance-bias / Class C protocol-level」 の 3 class taxonomy を確立と記載したが、 self-audit で:

| Class | Evidence | Status |
|---|---|---|
| **A. Cognitive-bias** (S9 で実証) | 1 scenario × N=3、 named callout で 100% recovery + smoking gun | ✅ **Valid、 named anti-attractor callout pattern を memory `feedback_named_callout_pattern.md` 候補に lift** |
| B. Affordance-bias (S6 想定) | **観測 invalid (= scenario design flaw)** | ❌ **Hypothesis only、 evidence base ゼロ。 valid retest 後に再判定** |
| C. Protocol-level (G12 既存) | 過去 batch 多数 | (本 batch とは無関係、 reference のみ) |

「介入 layer ladder」 (= prompt → schema → envelope → model) と「class identification rule」 自体は memory `feedback_envelope_layer_fix.md` の既存知見の拡張で valid だが、 **「affordance-bias は schema/envelope/model layer 必要」 の主張は evidence 不在**。 仮説 (= hypothesis) として保持、 batch 20 以降の valid retest で再評価。

## 5. Carry-over (= revised)

| Item | Severity | 工数 | 着手 trigger |
|---|---|---|---|
| **Router SP guidance revert** | (= self-audit fix) | 即時 | landed (= 本 commit) |
| S6 scenario re-design (= prompt を indexed docs content と semantic match) | LOW | ~0.3 day | batch 20 prep wave |
| R-RAG-srcread の真の存在検証 (= valid scenario で再測定) | MED | batch 20 で実施 | scenario re-design 完了後 |
| `recall` description 強化 (= 「semantic content questions」 example 追加候補) | MED | ~0.2 day | tool description 改善 wave、 ただし `reyn_src_read` description との分業設計が前提 |
| R1 (= reyn web interactive=False) | UX | 1 day | release-readiness wave (= 別 lump) |
| B17-S5-1 ctrl42 | LOW | (deferred) | phase 2 model selection |

## 6. Self-audit (= 当初版の誤りと真因)

### 当初の誤った推論 chain

1. batch 18 で S6 0/3 refuted を「R-RAG-srcread attractor」 と命名
2. batch 19 で router SP に「prefer recall over file_read」 guidance を追加
3. batch 19 retest で 0/3 refuted (= 改善ゼロ)
4. → 「prompt-layer fix exhausted、 affordance-bias attractor 確認、 schema/envelope/model layer escalation 必要」 と結論
5. → 原則 13 (= attractor class taxonomy) を確立、 retrospective + memory 追加

### 真因 (= self-audit で判明)

- 1〜5 のすべての step で **LLM trace を読んでいない、 tool description を読んでいない、 indexed content と prompt の semantic match を確認していない**
- 真因は **scenario design** で attractor 観測の前提条件 (= prompt が indexed docs の content と semantic match) を満たしていなかった
- LLM の routing は **正しかった** (= reyn_src_read description との textbook match)
- 私が batch 19 で追加した SP guidance は **正しい routing を妨害する逆方向の介入**

### Memory rule violation

memory `feedback_observe_before_speculate_llm.md` (= 「LLM への送信 payload (system prompt + tools + messages) を観測する infra を整える前に推測を積み上げない」) を 私自身が batch 19 retrospective 執筆時に違反。 **observation infra (= LLM trace dump) は存在していたのに、 retrospective を書くときに参照しなかった**。

### 教訓 (= 新 memory entry に lift)

**「retrospective 執筆前に必ず読む 3 つ」 (= pre-retrospective discipline)**:
1. 当該 scenario の **LLM trace dump** (= reasoning trace + tool_calls)
2. 関連 tool の **ToolDefinition description** (= LLM が見える signal)
3. **scenario design の前提条件** (= prompt と data source / tool affordance の semantic match)

これを satisfy しないと、 0/3 refuted を見て attractor 命名 → fix 試行 → 効果なし → class taxonomy 確立 という **誤った generalization chain** に陥る。

## 7. Verdict (= revised)

| 軸 | 判定 |
|---|---|
| Trajectory ✓ (= ≥50% verified rate) | S9 alone = 100%、 S6 invalid なので aggregate 計算保留 |
| Cognitive-bias attractor fix template (= named anti-attractor callout) | **✅ S9 で実証、 valid evidence、 transferable pattern** |
| Affordance-bias attractor の存在 / 介入 layer | ❌ **未検証、 valid retest が必要** |
| Pre-retrospective discipline (= LLM trace + tool description 必読) の確立 | ✅ **本 batch self-audit の真の学び** |

batch 19 の本当の価値は:
1. **S9 cognitive-bias の named callout fix template 確立** (= valid)
2. **Pre-retrospective discipline の operational lift** (= self-audit から)
3. **過剰一般化 trap の自己実例** (= 1 batch 1 scenario evidence で taxonomy 確立しかけた)

S6 affordance-bias は **valid retest がない仮説のまま**、 batch 20 以降で scenario re-design + retest で評価。
