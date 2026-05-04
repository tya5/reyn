# Batch 8 (cumulative fix verification + observation infra retest) — Prelude

> batch 7 wave で landing した 8 件の fix + 観測 infra 4 道具 の累積効果を、
> chat 経由 e2e で primary verify するのが primary 目的。 観測 batch = fix dispatch しない。

## 当時の Reyn 状態

| 項目 | 値 |
|---|---|
| Date | 2026-05-04 |
| main HEAD (batch 開始時) | `310e00f` (= batch 7 narrative integration 後) |
| Test suite | 938 passed / 2 xfailed |
| LiteLLM proxy | localhost:4000、 model `openai/gemini-2.5-flash-lite` |
| 観測 infra | 整備済 (= batch 7 で landing した 4 道具) |

## Batch 7 wave の到達点

batch 7 は「6 commit の累積 e2e 効果 verify」を想定して始まったが、
**観測 infra 整備 + care boundary 言語化** という設計レベルの成果に転化した
大規模 batch。 dogfood 中に 2 件の [HIGH] bug が発見され、その解消まで含めて
以下の commit が main に積まれた:

| 領域 | 内容 | 累積 test |
|---|---|---|
| 観測 infra | `REYN_LLM_TRACE_DUMP` + dogfood_trace 3+1 mode + llm_replay 4 mode + detect_attractor | +22 |
| Fix: router enum | `invoke_skill.name` に enum 制約 + system prompt flat skill list (`9ee6ae1`) | +16 |
| Fix: preprocessor anyOf | `_get_at_path` で anyOf/oneOf/allOf handling (`3cbe983`) | +12 |
| Fix: B8-NEW-1 | skill_improver に stdlib skill paths の read 許可 (`f229f6c`) | +8 |
| Fix: B8-NEW-2 | eval_builder の compute_paths を trusted python step 宣言 (`ed9de6c`) | +6 |
| Fix: Option F | empty stop detect + event + explicit failure UX、 retry なし (`48125ab` + `0a274fd`) | +18 |
| Fix: 先行 wave | eval_builder D1+D2+D3a / skill_improver B5-M2 / chat trusted python / Workspace glob | +103 |
| Docs | ADR 0021 + care boundary (en+ja) + 5 feedback memory + RETRO-H1〜H4 | 0 |

合計 **+185 test** 増分 (753 → 938)、 0 regression。
main HEAD は `310e00f` (= batch 7 retrospective + findings + giveup-tracker 整備後)。

### 観測 infra (batch 7 で整備)

| ツール | 機能 |
|---|---|
| `REYN_LLM_TRACE_DUMP=<path>` | LLM call full payload を JSONL dump |
| `dogfood_trace.py llm-payloads` | payload 一覧 (caller / msgs / finish) |
| `dogfood_trace.py llm-detail <id>` | 特定 request の全 payload 表示 |
| `dogfood_trace.py llm-tools-schema <id>` | tools schema 表示 |
| `dogfood_trace.py --trace a,b,...` | multi-trace merge |
| `llm_replay.py <id> --trace <path>` | payload を litellm 直接 replay |
| `llm_replay.py --patch '...'` | payload 改変 replay (fix 効果を landing 前に verify) |
| `llm_replay.py --diff` | original vs replay 比較 |
| `llm_replay.py --n <N>` | N-shot で確率分布測定 |
| `llm_replay.py --model <override>` | 別 model で replay (G4 spike 等) |
| `detect_attractor.py` | empty stop / enum violation / hallucinate name の自動検出 |

### 主要 finding 系譜

- **B7-S1-observation**: chain 起動せず、router dot-notation bug (`B7-NEW-1`) 発見
- **B7-S5b**: preprocessor anyOf schema non-support (`B7-S5b-NEW`) 発見
- **B7-RETRO-H1**: router enum 制約不在を観測で確定 → enum fix dispatch
- **B7-RETRO-H2**: eval_builder input field hallucinate は describe_skill skip 起因
- **B7-RETRO-H3**: H3 unblock の真の blocker は B7-S5b (= B7-NEW-1 ではなかった、 推測訂正)
- **B7-RETRO-H4**: MUST rule は system prompt に確かに injected、 attractor は真の確率的挙動
- **B7-G12-empty-stop-frequency**: 同 payload N=10 で 50% empty-stop → 確率的 glitch と確定
- **B7-S1-fresh-retest**: router enum fix 後に chain 進行、 ただし B8-NEW-1 / B8-NEW-2 で copy_to_work 停止

## Batch 8 が必要な理由

batch 7 の fix wave (= 8 件) は **path 1 (--patch replay) で部分 verify、 path 2 (= fresh dogfood) で partial completion** まで到達した。 しかし e2e 完走は未確認:

1. **chain 完走 e2e**: B8-NEW-1 / B8-NEW-2 fix 後の copy_to_work → run_and_eval → finalize 完走が未 verify
2. **Option F の実 LLM 観測**: empty stop に対する clean failure UX の動作確認が未達
3. **B7-RETRO-H3 unblock**: preprocessor anyOf fix 後に copy_to_work に到達し、 実 LLM が `data.validation` を参照するかの観測が未達
4. **describe_skill 強制 (H2 第 2 層)**: input field hallucinate 抑制の fix が別 wave で実装中、 e2e effect 未 verify
5. **care boundary framework の運用感覚**: ADR 0021 + care boundary doc が dogfood 実行で機能するか

path 2 retest (`B7-S1-fresh-retest.md`) の到達点:
- router 通過 ✅ (dot-notation hallucinate 消失)
- prepare 完走 ✅
- copy_to_work で permission_denied 停止 ❌ → B8-NEW-1 fix 後に解消済

batch 8 は「B8-NEW-1 / B8-NEW-2 fix 含む全 cumulative fix の e2e effect」 を確認する batch。

## Prediction 設計

batch 7 retrospective の教訓を反映し、 **4 区分** に拡張:

- `verified`: top probability category が prediction と一致
- `inconclusive`: 到達できたが観測が曖昧、 判断不能
- `refuted`: top probability category が prediction と不一致
- `blocked`: 前段 bug / infra 問題で観測経路自体が機能せず

`blocked` の base rate:
- chain 完走系 (= 前段 stability 低): 20-30%
- 単独 skill 経由 (= 前段 stability 中): 10-20%
- Option F 等 infra 系 (= 前段 stability 高): 5-10%

参照: `prediction-calibration.md` (= 別 sonnet 並走で land 中)

## 当時の心境

batch 7 では「LLM がおかしい」と言いたくなる場面が何度もあった。 しかし
user 介入 (「llm が見たもの確認した?」) で観測 infra 整備という根本的な
対処に引き戻された。 道具ができてからの iteration speed は確かに変わった。

batch 8 は「道具を使って、累積 fix の効果を静かに確認する batch」のつもり。
派手な発見はないかもしれない。 chain が完走すれば「chat 経由で skill_improver
が動く」という Reyn の primary use case が初めて完全に機能した証拠になる。
それはそれで、地味だが確かな前進。

fix が足りなければまた別の blocker が見つかる。 それはそれで次の wave の起点。
dogfood は予測通りには進まないが、 だからこそ観測に価値がある。

> 「LLM がおかしい」と言う前に、LLM に渡したものを観測する道具がある状態でも
> 同じ結論が出るか確認する。 batch 8 はその初めての実践。

— assistant internal state、 batch 8 開始直前

## 参照リンク (cross-ref)

- batch 7 prelude: `../2026-05-04-batch-7-post-infra-verify/prelude.md`
- batch 7 findings: `../2026-05-04-batch-7-post-infra-verify/findings.md`
- batch 7 retrospective: `../2026-05-04-batch-7-post-infra-verify/retrospective.md`
- B7-S1-fresh-retest: `../2026-05-04-batch-7-post-infra-verify/findings/B7-S1-fresh-retest.md`
- B7-G12-empty-stop-frequency: `../2026-05-04-batch-7-post-infra-verify/findings/B7-G12-empty-stop-frequency.md`
- B7-RETRO-H3: `../2026-05-04-batch-7-post-infra-verify/findings/B7-RETRO-H3-validation-field.md`
- ADR 0021 (Option F): `../../en/decisions/0021-g12-attractor-structural-fix-design.md`
- giveup-tracker: `../giveup-tracker.md`
