# Batch 19 — Retrospective (Revised after self-audit)

> **2026-05-10 self-audit revision**: 当初版で原則 13 (= attractor class
> taxonomy) を batch 19 結論として確立したが、 user 指摘 + LLM trace +
> tool description re-read で **S6 評価が scenario design flaw に基づく
> 誤った推論** と判明。 当初版は §3 self-audit 部分に保存、 本文は
> revised conclusion を記載。 真の batch 19 学びは **(1) S9 named
> anti-attractor callout pattern 確立 + (2) pre-retrospective discipline の
> 自己実例による operational lift**。

---

## 1. Expected vs actual (revised)

| 項目 | 予測 | 実際 (revised) |
|---|---|---|
| S9 verified | 65% | **100%** (3/3) — valid、 cognitive-bias fix template 確立 |
| S6 verified | 50% | 0% (3/3 refuted) **but scenario design flaw、 attractor 観測 invalid** |
| 新原則 確立 | 0-1 | **1 (= named anti-attractor callout pattern)、 当初主張の attractor class taxonomy は evidence 不足で downgrade** |
| Pre-retrospective discipline 違反 | (想定外) | **発生 → self-audit で判明 → 教訓 lift** |

---

## 2. Turning points (revised)

### TP1: S9 で named anti-attractor callout が 100% compliance を達成 (= valid)

batch 18 retrospective で「strategy.md prompt 強化 (= boolean flag explicit priority)」 を carry-over fix と提案、 batch 19 で landed (= `ef70aef` の `phases/strategy.md` 編集)。 想定 65% verified → **actual 100%**、 run 2 では LLM が `$0.0003 USD` を自己引用しつつ abort 出力 (= smoking gun)。

**学び (= valid)**: cognitive-bias 系 attractor (= LLM が input data を見ているが evidence の比重を間違える pattern) に対して、 **named anti-attractor callout** (= 「Common attractor to avoid: when X, do NOT Y. Z wins over W.」 形式) は prompt-layer で effective、 100% compliance を達成。 batch 6-12 の MUST rule 60-80% から大幅向上。

これは valid evidence で、 transferable な fix template として確立。

### TP2: S6 self-audit で当初結論が flawed と判明

当初 batch 19 で S6 0/3 refuted を「R-RAG-srcread affordance-bias attractor、 prompt-layer fix exhausted」 と結論し、 原則 13 を確立。 user 指摘で context 分析を skip したと判明、 LLM trace + tool description を re-read した結果:

| 確認 | 結果 |
|---|---|
| LLM trace (= `/tmp/reyn_s6_b19/run_*.jsonl`) | 全 3 run で turn 1 = `reyn_src_read("README.md")`、 turn 2 = recall の正確な description text |
| `reyn_src_read` description | **「Use this for any 'how does Reyn / how does Reyn's X work?' question — Reyn's source is the authoritative answer」** + 「Start with reyn_src_read('README.md') for an overview」 |
| `recall` description | 「Search indexed sources by natural-language query」 — generic、 「implementation」 query への explicit signal なし |
| `reyn_docs` indexed content | concept doc only、 implementation 詳細含まない |

**真因**: scenario design flaw。 prompt 「How is recall **implemented**?」 = code-reading query で、 expected answer は source file に存在、 indexed concept docs には不在。 LLM が `reyn_src_read` を選んだのは **tool description との textbook match による正しい routing**。

私が batch 19 で追加した router SP guidance (= 「prefer recall over file_read for 'how is X' questions」) は **正しい routing を override する逆方向の介入** で、 commit `1c5856d` で landing したが本 retrospective revision で **revert**。

### TP3: Pre-retrospective discipline 違反の自己実例

memory `feedback_observe_before_speculate_llm.md` (= 「LLM への送信 payload を観測する infra を整える前に推測を積み上げない」) を私自身が batch 19 retrospective 執筆時に違反。 **trace dump は存在していたのに、 retrospective を書くときに参照しなかった**。

これが batch 19 の真の学び: **observation infra が存在しても、 retrospective 執筆 discipline がないと過剰一般化 trap に陥る**。

---

## 3. Self-audit — 当初の誤った推論 chain

### Step-by-step trace

1. batch 18 で S6 0/3 refuted を「R-RAG-srcread attractor」 と命名 (= scenario design 妥当性を疑わなかった)
2. batch 19 で router SP に「prefer recall over file_read」 guidance を追加 (= tool description との conflict を確認しなかった)
3. batch 19 retest で 0/3 refuted (= 改善ゼロ)
4. → 「prompt-layer fix exhausted、 affordance-bias attractor 確認」 と結論 (= LLM trace を読まなかった)
5. → schema/envelope/model layer escalation が必要と推論
6. → 原則 13 (= attractor class taxonomy) を確立、 retrospective + memory 追加 (= 1 scenario evidence で taxonomy 一般化)

### 各 step の miss

| Step | Skip した分析 | 影響 |
|---|---|---|
| 1 | scenario design (= prompt と indexed content の semantic match) 妥当性 | 後続全 step の前提が崩れた |
| 2 | tool description 同士の conflict (= reyn_src_read の specialised claim) | 逆方向 SP fix を landing |
| 3 | LLM trace の reasoning content | 「fix exhausted」 と誤断 |
| 4 | LLM が選んだ tool の rationale | attractor 命名が誤り |
| 6 | evidence base の sample 数 | 1 scenario で taxonomy 一般化 |

### 教訓 (= 新 memory entry に lift)

**Pre-retrospective discipline (= 「retrospective 執筆前に必ず読む 3 つ」)**:

1. 当該 scenario の **LLM trace dump** (= reasoning trace + tool_calls + content)
2. 関連 tool の **ToolDefinition description** (= LLM が見える signal)
3. **scenario design の前提条件** (= prompt と data source / tool affordance の semantic match)

これを satisfy しないと、 0/3 refuted を見て attractor 命名 → fix 試行 → 効果なし → class taxonomy 確立 という **誤った generalization chain** に陥る。

memory `feedback_pre_retrospective_discipline.md` 候補で operationalize。

---

## 4. 確立された原則 (revised)

### Valid (S9 evidence)

**Named anti-attractor callout pattern**:
- 適用対象: cognitive-bias 系 LLM 行動 attractor (= input data は見ているが evidence weighting で間違える)
- Format: 「Common attractor to avoid: when X, do NOT Y. Z wins over W.」
- Compliance rate: ~100% (= S9 で実証)
- Transfer scope: 同 class の他 attractor (= 「flag を ignore して metric に anchor」 系) に generalizable

### Hypothesis only (S6 not valid evidence)

**Affordance-bias attractor class** + **介入 layer ladder**:
- 当初 batch 19 で「prompt-layer fix exhausted、 schema/envelope/model escalation 必要」 と結論したが、 S6 は invalid evidence
- 仮説 (= hypothesis) として memory に保持、 batch 20 以降の valid scenario で再評価
- memory `feedback_attractor_class_taxonomy.md` を「hypothesis、 evidence pending」 ステータスに downgrade

### Operational (self-audit から)

**Pre-retrospective discipline**:
- LLM trace + tool description + scenario design 前提を retrospective 執筆前に必ず確認
- memory `feedback_pre_retrospective_discipline.md` で operationalize

---

## 5. Carry-over (revised)

| Item | Status | 工数 | 着手 trigger |
|---|---|---|---|
| Router SP R-RAG-srcread guidance revert | landed (本 self-audit commit) | — | — |
| Replay fixture re-record | landed (本 commit) | — | — |
| S6 scenario re-design (= indexed content と prompt の semantic match) | open | ~0.3 day | batch 20 prep |
| R-RAG-srcread の真の存在検証 (= valid scenario で再測定) | open | batch 20 含む | scenario re-design 後 |
| `recall` description 強化 (= 「semantic content questions」 example 追加) | candidate | ~0.2 day | tool description 改善 wave |
| Pre-retrospective discipline memory entry | landed (本 commit) | — | — |
| 原則 13 memory downgrade (= Class B → hypothesis) | landed (本 commit) | — | — |
| R1 (= reyn web interactive=False) | open | 1 day | release-readiness wave |
| B17-S5-1 ctrl42 | deferred | (— ) | phase 2 model selection |

---

## 6. Conclusion (revised)

batch 19 は **真の学び 2 件 + self-audit 1 件** で完了:

1. **Cognitive-bias attractor の named callout fix template** (= S9、 100% compliance、 transferable) — valid evidence で確立
2. **Self-audit による過剰一般化 trap の発見** — 1 batch / 1 scenario evidence で taxonomy 一般化しかけた、 user 指摘で軌道修正
3. **Pre-retrospective discipline の operational lift** — observation infra が存在しても discipline がないと無効化

S6 affordance-bias は **valid retest がない仮説のまま**、 scenario re-design 後の batch 20 で再評価。 当初 retrospective で確立した 「原則 13 attractor class taxonomy」 は **Class A (cognitive-bias = named callout) のみ valid、 Class B (affordance-bias) は hypothesis pending** に downgrade。

dogfood discipline framework の進化:
- batch 17: structural pre-check 必須
- batch 18: structural × behavioral 軸分離 (原則 11) + verdict false-attribution discipline (原則 12)
- batch 19 (revised): **named anti-attractor callout pattern (= cognitive-bias 限定 valid)** + **pre-retrospective discipline** (= dogfood agent の self-audit infra)

「production grade landed」 narrative は batch 18 で release-blocker 解消 + batch 19 (revised) で **cognitive-bias 系 fix template + pre-retrospective discipline** が確立、 1.0 OSS launch narrative は **「framework foundation + headline scenario green + cognitive-bias fix template + dogfood discipline self-correction infra」** で defendable。 affordance-bias の generalize は post-1.0 valid scenario で再評価。

self-audit の最大の学び: **「sober discipline」 という言葉を retrospective に書くだけでなく、 retrospective を書く前に discipline を実行する**。 observation infra が完備されていても、 執筆 phase で skip すると過剰一般化に直行する。 これは agent-driven dogfood の continuing risk。
