# Batch 7 (post-infra-fix verification + observation infra) — Findings

> 5 scenario + 4 retroactive hypothesis verification + 6 commit fix wave。
> 表面的に scenario verdict は blocked / refuted / partial 多数だったが、
> dogfood の真価として **観測 infra 整備 + 推測スタック解体 + 新 fix 連鎖**
> という構造的成果が大きかった batch。

## Summary table

### Scenario verdicts (= scenarios.md S1-S5)

| Scenario | 種別 | Verdict | 主要発見 |
|---|---|---|---|
| [S1](findings/B7-S1-observation.md) | 6 commit 統合効果 verify (primary) | **blocked** | 新 bug **B7-NEW-1**: router LLM が `skill_improver で direct_llm を` を dot-notation `skill_improver.direct_llm` と誤解釈、 chain 起動せず |
| [S2](findings/B7-S2-G3-dedupe-retest.md) | B5-M1 G3 dedupe retest | **verified (partial)** | dedupe event 観測 ✅、 ただし chain 未起動で完走 verify 未達 |
| [S3](findings/B7-S3-B4M1-path-retest.md) | B4-M1 eval.md path retest | **inconclusive** | prepare phase 未到達で観測不能 |
| [S4](findings/B7-S4-B6S1M1-final-verify.md) | B6-S1-M1 仮説 (a) 実 LLM verify | **inconclusive** | copy_to_work 未到達で観測不能 |
| [S5a](findings/B7-S5a-eval-builder-natural.md) | eval_builder 自然言語直接 invoke | **refuted** | router が skill 名 hallucinate (`eval_builder.eval_md`) |
| [S5b](findings/B7-S5b-eval-builder-structured.md) | eval_builder CLI 構造データ invoke | **refuted** | 新 bug **B7-S5b-NEW**: `preprocessor_typing.py` の anyOf union schema 非対応、 `e6de782` 由来 regression |

### Retroactive verifications (= 観測 infra 整備後)

| Hypothesis | NEW Verdict | 観測で確定したこと |
|---|---|---|
| [RETRO-H1](findings/B7-RETRO-H1-router-dot-notation.md) (B7-NEW-1) | **verified** | `invoke_skill.name` field に **enum 制約なし**、 system prompt に skill 個別名 list なし → LLM zero-shot pattern match で hallucinate |
| [RETRO-H2](findings/B7-RETRO-H2-eval-builder-hallucinate.md) (B7-S5a) | **partially verified** | skill 名 hallucinate は H1 と同因。 input field hallucinate は describe_skill skip 起因。 eval_builder failure は B7-S5b (independent) |
| [RETRO-H3](findings/B7-RETRO-H3-validation-field.md) (B6-S1-M1 仮説 a) | **prerequisite blocked** | copy_to_work 未到達、 真の blocker は B7-S5b (= B7-NEW-1 ではなかった、 過去推測訂正) |
| [RETRO-H4](findings/B7-RETRO-H4-attractor-prompt-evidence.md) (G12 attractor) | **verified** | MUST rule は **system prompt に確かに injected されている** (= 過剰 consolidation 仮説 refuted)。 LLM が rule を見たうえで `finish_reason=stop` で空 response = 真の意味の attractor |

### Fix verify

| Path | 経路 | Verdict | 結果 |
|---|---|---|---|
| [path 1](findings/B7-RETRO-H1-fix-verify.md) | `--patch` で payload 改変 → litellm replay | **fix effective** | hallucination 57% → 0% (= router enum fix の effect 確定) |
| [path 2](findings/B7-S1-fresh-retest.md) | fresh dogfood で実 e2e | **partial** | router enum 効果 ✅ + chain 進行、 ただし新 blocker (B8-NEW-1 / B8-NEW-2) で完走未達 |

### Measurement

| Measurement | 結果 | Implication |
|---|---|---|
| [G12 empty-stop frequency](findings/B7-G12-empty-stop-frequency.md) | 同 payload N=10 で 5/10 (= 50%) empty stop | 確率的 (probabilistic) glitch、 context 不全でない → ADR 0021 Option B 却下 + Option F 採用の根拠 |

## Round 別 narrative

### Round 1: scenarios.md A1-A3 実行 (S1-S5)

5 scenario のうち 4 が `blocked` / `inconclusive` / `refuted` で expected hit rate 大幅 miss。 当時は「6 commit fix を施したのに何故 chain 起動しないのか」 と困惑。 結果的に 2 件の **新 HIGH bug** (B7-NEW-1: router dot-notation / B7-S5b: preprocessor anyOf) を発見、 これが本 batch の最大の成果。

### Round 2: 推測スタックへの user 介入

私 (= main agent) は B7-NEW-1 / B7-S5a の真因について **enum 制約不在** / **system prompt の category-only 表記** / **LLM の dot-notation 誤解釈** 等の仮説を組み立て始めた。 user 指摘:

> あなたの分析を見てると推測ばかりが並んでいて納得感がないのです。 llm がおかしいというからには llm に渡しているコンテキストが問題ないことを確認すべきですが、 それをやってますか？

→ 私は yes と即答できなかった。 既存 events log には LLM 送信 payload を記録する仕組みなく、 「LLM が何を見たか」 を観測する術なし。

### Round 3: 観測 infra 整備 (~1 day)

3 道具を sonnet 並列 dispatch で整備:

1. **`REYN_LLM_TRACE_DUMP=<path>`** env var で LLM call の full payload を JSONL dump (commit `bffd2c4`)
2. **`scripts/dogfood_trace.py`** に `llm-payloads / llm-detail / llm-tools-schema` 3 mode 追加 (commit `269bdb6`)、 後に multi-trace merge も追加 (commit `2330299`)
3. **`scripts/llm_replay.py`** で dump payload を litellm 直接 replay (commit `14327be`)、 後に `--patch` (`eeb8ed9`) + `--diff` (`32458f1`) 追加
4. **`scripts/detect_attractor.py`** で attractor 自動検出 (commit `f08a5b3`、 3 heuristic)

これらは Reyn の primary debug infrastructure として整備、 production user (= skill 開発者) も使える形。

### Round 4: retroactive 検証で推測 4 件中 1.5 件訂正

整備した道具で過去推測 4 件を retroactively verify (= sonnet 1 体に dispatch、 commit `cfe8f78`):

| 過去推測 | 観測 verdict |
|---|---|
| 「enum 制約不在問題?」 | **Confirmed**: enum 無し |
| 「過剰 consolidation で rule 削れた」 | **Refuted**: rule は injected 済 |
| 「H3 観測不能は B7-NEW-1 が blocker」 | **Partially wrong**: 真の blocker は B7-S5b |
| 「LLM が rule 見たうえで non-honor」 | **Confirmed**: payload で確認 |

→ 道具無しで fix 設計してたら間違った fix が積まれていた。 memory `feedback_observe_before_speculate_llm.md` を 4 つ目の feedback memory として記録。

### Round 5: 観測ベース fix wave 連鎖

retro 結果から fix priority が観測 evidence で確定:

1. **router enum fix** (commit `9ee6ae1`): `invoke_skill.name` enum + system prompt flat skill list inject
2. **preprocessor anyOf fix** (commit `3cbe983`): `_get_at_path` で anyOf/oneOf/allOf handling
3. **B8-NEW-1** (commit `f229f6c`): skill_improver の stdlib path read 許可
4. **B8-NEW-2** (commit `ed9de6c`): eval_builder の trusted python step declare

router enum fix は 2 経路で verify:
- path 1 (--patch replay): hallucination 57% → 0% ✅
- path 2 (fresh dogfood): chain 進行確認、 ただし次層 blocker (B8-NEW-1 / B8-NEW-2) 露呈 → 同 session 内で fix landing

### Round 6: G12 empty-stop measurement + Option F redirect

G12 attractor について、 当初 ADR 0021 で Option B (= retry) を短期推奨として提案。 user 質問:

> ちょっと待って、 問題がよくわからないんだけど、 llm が空文字停止したことを問題視してるの？空文字を導いたコンテキストが問題だと言ってるの？

→ 議論で軌道修正。 既存 dump で `llm_replay.py --n 10` 測定 (commit `5549bac`):
- 同 payload 10 回中 **5 回 empty stop / 5 回 通常応答** = 50% probabilistic
- = context 単独で deterministic に empty を引き起こすわけではない、 model 内部 randomness が proximate cause

user 原則:

> コンテキストに問題がないのに空文字だった場合のケース、 これは llm の問題であって、 reyn で過剰ケアすべきではない。 retry すべきでない

→ Option B / Option C (= retry / escalation) を **却下**、 **Option F** (= detect + event + clean failure UX、 no retry) を採用 (commit `48125ab` + `0a274fd`)。 G12 を giveup-tracker で **C1 (model-capability-tradeoff、 主因)** として登録、 真の解は user-side G4 spike。

### Round 7: care boundary 言語化 + doc 化

Round 6 の議論で「Reyn の care 範囲」 が言語化された:

> 「LLM が判断する environment」 は Reyn が作る (= pre-call structural)、
> 「LLM の判断結果」 は Reyn が触らない (= post-call observe-only)、
> 「LLM への注文」 は最小限 (= prompt rule の bloat 注意)

3 区分 framework (= structural / behavioral / gray) を:
- `docs/en/concepts/care-boundary.md` (= 公開 concept doc)
- `docs/ja/concepts/care-boundary.md` (= 翻訳)
- 5 つ目の feedback memory (= `feedback_reyn_care_boundary.md`)
- CLAUDE.md cross-reference

として永続化 (commit `0d97222`)。

## 累積 commit (= ~30 件、 main HEAD `0d97222`)

| 領域 | 内容 |
|---|---|
| Fix wave | router enum / preprocessor anyOf / B8-NEW-1+2 / eval_builder D1+D2+D3a / Option F / chat trusted python / Workspace glob perm / B5-M2 / others |
| 観測 infra | LLM payload trace dump + dogfood_trace 3 mode + multi-trace merge + llm_replay (--patch / --diff / --n / --model) + detect_attractor |
| 検証 doc | RETRO-H1〜H4 + path 1/2 retro verify + G12 measurement + B7-S1-S5 raw observations |
| 設計 / philosophy | ADR 0021 (Option F accepted) + care boundary concepts (en+ja) + 5 feedback memory |

合計 **+185 test** 増分 (753 → 938)、 0 regression。

## A4 review (= user 感覚との差分)

- 最大の成果は **観測 infra 整備 + care boundary 言語化**、 当初想定の「6 commit fix の e2e 効果検証」 は副次的にしか達成せず、 ただ **新 bug 4 件発見** で fix 連鎖を triggered → 結果として大規模な構造的成果
- prediction 形式: 分布形式 prediction を試行、 「`blocked` / `inconclusive` カテゴリを prediction に含めていなかった」 痛感、 batch 8 以降の prediction 設計で **4 区分 (verified / inconclusive / refuted / blocked)** に拡張すべき教訓
- G12 議論で user の「Reyn 過剰ケアしない」 原則が明確化、 これが care boundary doc 化の trigger
- 道具と原則が揃ったので、 batch 8 以降は **観測ベースの fix 設計サイクル** が成立する状態

## 残懸念点 + 次 wave 候補

| 優先 | 内容 |
|---|---|
| 高 | batch 8 retest (= 別 session で 累積 fix の e2e effect 観測) |
| 中 | empty stop の context 要因診断 (= `--patch` で system prompt / messages 改変 → rate 探り) |
| 中 | describe_skill 強制 (= H2 第 2 層 input field hallucinate) |
| user-side | proxy 強モデル追加 → Wave 3 G4 spike 解禁 |
