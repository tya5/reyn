# Batch 18 — RAG Fix Retest Findings (Aggregate)

> 4 scenarios × N=3 (S5 拡張 N=12) = 12 primary runs。 main HEAD `9681096`。
> Headline (= S5) が batch 17 0/5 から **3/3 verified** で full recovery、
> ただし S6 / S8 / S9 で新 LLM-behavioral attractor が surface して
> 25% (= 3/12) primary verified rate にとどまる。
> **Structural axis 100%、 behavioral axis 25%、 batch 14 milestone (= 70%+) 未達**。

## 1. Per-Scenario Summary

| Scenario | 予測 verified | 実測 verified | Verdict 4-tuple | Brier (4-class) | Structural | New attractors / bugs |
|---|---|---|---|---|---|---|
| **S5** recall via chat (HEADLINE) | 80% | **100%** (3/3) | 3/0/0/0 | **0.067** | ✓ | B18-S5-1 (envelope 40KB vector leak、 MED)、 B17-S5-1 ctrl42 ~17% (deferred) |
| S6 multi-source recall | 70% | 0% (0/3) | 0/3/0/0 | 0.264 | ✓ recall in catalog | R-RAG-srcread (= reyn_src_read 親和性) |
| S8 drop_source via chat | 75% | 0% (0/3) | 0/0/3/0 | 1.505 | ✓ 3 fixes 全 verified | R1 (= `reyn web` PermissionResolver(interactive=False) で ask gate dead) |
| S9 cost preflight gate | 70% | 0% (0/3) | 0/3/0/0 | 1.055 | ✓ abort candidate 出現 | B18-S9-1 HIGH (LLM が `threshold_exceeded: true` boolean を ignore して `0.0003` numeric に anchor) |

**Aggregate**:
- primary verified: **3/12 = 25%** (= batch 14 milestone 70%+ 未達)
- 拡張 verified (= S5 N=12 含む): **12/21 = 57%**
- mean Brier: **(0.067 + 0.264 + 1.505 + 1.055) / 4 = 0.723** (vs batch 17 = ~0.32 → 悪化、 ただし悪化原因は predict の楽観バイアス)
- structural pre-check: **4/4 = 100%** (= fix wave 5 件はすべて intended layer で landed)

## 2. Critical Insight: Structural ≠ Behavioral 観測の完全実証

batch 17 retrospective 教訓 10 (= structural pre-check と attractor 観測は別軸) が batch 18 で **3 scenario 連続で実証**:

| Scenario | Structural fix landed | Behavioral verified ?  | 真因 |
|---|---|---|---|
| S6 | ✓ recall in tools=、 dispatch frozenset 含む | ✗ | LLM が `reyn_src_read` を選好 (= 別 tool に流れる attractor) |
| S8 | ✓ index_drop=True、 build_tools 含む、 mtime poll | ✗ | `reyn web` の interactive=False で ask cycle が deny に short-circuit (= verification path 自体が unreachable) |
| S9 | ✓ abort candidate を candidate_outputs 出現 | ✗ | gemini が boolean flag (`threshold_exceeded: true`) を numeric value (`0.0003`) より弱く weight |

**学び**: 「fix が structural に landed = behavioral verified rate 上方修正」 という予測 logic は **prior wrong**。 batch 17 で「acceptance criteria に layer-wiring boxes 追加」 と提案したのは structural axis のみで behavioral attractor は別軸の base rate 測定が必要。 batch 18 で **新原則 11 (= structural ≠ behavioral 予測 axis 分離)** を確立。

## 3. New Bug Catalog

### B18-S9-1 [HIGH]: LLM が boolean flag を numeric value より弱く weight する

- **Symptom**: cost_preflight artifact の `threshold_exceeded: true` を無視、 `estimated_cost_usd: 0.0003` の小ささに anchor して abort 判断せず
- **Run 3 reasoning** (= LLM 自己報告): 「the number of chunks does not exceed the warning threshold」 (= input data と直接矛盾)
- **Affected layer**: stdlib `index_docs` skill `phases/strategy.md` instructions (= OS / chunkers untouched)
- **Fix**: strategy.md で「**`threshold_exceeded: true` を見たら必ず abort、 numeric cost は ignore**」 と明示 (= single-file change)
- **Severity rationale**: cost preflight UX gap fix B (= ADR-0033 §2.1 UX gap fix B) の本質的目的 (= 「project 全体 index で $200 課金」 事故防止) が construct-validity 上達成されない

### B18-S5-1 [MED]: tool result envelope に raw vector floats が 40KB/call leak

- **Symptom**: `recall` tool result が next-turn LLM context に embedding vector (= 1536 floats × ~5 chunks = ~40KB JSON) として serialised
- **Affected layer**: recall macro op handler (= sub_op merge step で chunk meta の vector field を strip していない)
- **Fix candidate**: recall result から `vector` field を strip (= top_k 後はもう不要)、 `metadata` のみ retain
- **Severity**: MED (= LLM context 圧迫 + token cost、 ただし chat 1 turn では破綻しない)、 production user impact は long session でのみ surface

### R-RAG-srcread [新 attractor、 prompt-level fix candidate]

- **Description**: 「How is X implemented?」 prompt で LLM が semantic recall より file system tree の `reyn_src_read` を選好
- **Observed rate**: 3/3 (= 100% S6)
- **Mitigation candidate**: router system prompt で「Indexed sources contain semantic chunks; for 'how is X implemented' style questions about content (not file structure), prefer recall over file ops」 と explicit guidance
- **Severity**: MED (= 用途 split に関する LLM behaviour、 fix は prompt-level)

### R-RAG-numerical-vs-flag-bias [新 attractor、 systemic]

- **Description**: gemini-flash-lite 系で boolean flag より numeric value を強く weight する pattern
- **Observed rate**: 3/3 S9 (= small sample、 batch 19 で systematic 測定候補)
- **Mitigation candidate**: phase 1 strategy で boolean flag の影響を numeric data よりも上位に位置付ける (= prompt structuring)
- **Severity**: HIGH (= cost preflight 以外の boolean gate 一般に拡張可能性)

## 4. Carry-over

| Item | Status | 着手 trigger |
|---|---|---|
| B18-S9-1 strategy.md prompt 強化 | open、 single-file fix | batch 19 prep wave (= 1 day 想定) |
| B18-S5-1 recall envelope vector strip | open、 op handler 1 file change | batch 19 prep or sweep wave |
| R-RAG-srcread prompt-level guidance | open | batch 19 prelude で R-attractor table に追加 + prompt fix dispatch |
| R1 reyn web interactive=False (S8) | open | release-readiness UX wave (= 別 wave、 universal secret + auto-approve env path 整理) |
| B17-S5-1 ctrl42 (~17% rate) | deferred | phase 2 model selection (= strong model 切替時に再評価) |
| Test infra fix (= dogfood_rag_helper NaN bug + sitecustomize) | landed (batch 18 中) | — |

## 5. Verdict

| 軸 | 判定 |
|---|---|
| Structural fix 効果 (= fix wave 5 件 + 配線 fix 1 件) | ✓ **100% intended layer で landed**、 全 4 scenario の structural pre-check pass |
| Headline (S5 = production-blocker) recovery | ✓ **0/5 (B17) → 3/3 (B18)、 拡張 N=12 で 83% verified、 Brier 0.575 → 0.067** |
| Batch 14 milestone (= 70%+ verified rate) 復帰 | ✗ **3/12 = 25% primary** (拡張で 57%)、 未達 |
| Production grade narrative | partial — **headline ✓ + structural ✓ で release blocker は close、 secondary scenarios の attractor fix は別 wave** |

batch 17 「production grade landed」 撤回からの再構築は **headline 軸では成功、 secondary 軸では新 layer の課題が surface して継続 wave 必要**。 1.0 release narrative は「framework foundation + headline scenario green」 で OSS launch 可能、 secondary attractor fix は 1.0 後 1.1 fast follow で対応する判断が現実的。
