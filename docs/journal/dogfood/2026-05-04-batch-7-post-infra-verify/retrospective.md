# Batch 7 (post-infra-fix verification) — Retrospective

> 当初は「batch 6 wave で landing した 6 commit の e2e 効果を verify する short batch」 のつもりが、 dogfood 中に user 介入で **観測 infra 整備 + care boundary 言語化** という設計レベルの成果へ転化した、 reyn にとって構造的に重要な batch。

## 想定と現実のずれ

### 開始時の想定 (= scenarios.md A1)

`prelude.md` 当時の私の事前認識:
- 6 commit 累積 fix で chain 完走するはず (S1)
- B5-M1 G3 dedupe / B4-M1 path 等の MED 残件を観測で確定 (S2-S5)
- prediction 分布形式を試行 (= calibration 改善期待)

### 実際の進行

| 想定 | 現実 |
|---|---|
| chain 完走 verify | **chain 起動せず** で blocked、 router 段階で新 bug 発見 |
| MED 残件観測で fix 確定 | MED は前段で blocked → 観測経路自体が機能せず |
| 分布形式 prediction で精度向上 | `blocked` カテゴリ漏れで mass miss |
| short batch (= 5 scenario + 並列実行) | observation infra 整備 + retroactive 検証 + 設計議論で **超大規模 batch** に |

= dogfood が **観測した予想外の事象が batch の主軸を再定義する** 典型例。

## ターニングポイント 3 つ

### TP1: user 「llm が見たもの確認した?」

私が B7-NEW-1 / B7-S5a の真因について推測スタックを組み始めた時の介入:

> あなたの分析を見てると推測ばかりが並んでいて納得感がないのです。 llm がおかしいというからには llm に渡しているコンテキストが問題ないことを確認すべきですが、 それをやってますか？

これで観測 infra 整備が batch の primary task に redirect された。 結果的に Reyn の永続的な debug infrastructure (= REYN_LLM_TRACE_DUMP + dogfood_trace + llm_replay + detect_attractor) が産まれた。

教訓: **「LLM がおかしい」 と言うとき、 LLM に渡したコンテキストを観測する道具がなければ、 それは推測である**。 道具を作ってから疑うのが筋。

### TP2: user 「Reyn で過剰ケアすべきではない」

ADR 0021 で Option B (= empty stop に retry で対処) を短期推奨として提案した時の介入:

> コンテキストに問題がないのに空文字だった場合のケース、 これは llm の問題であって、 reyn で過剰ケアすべきではない。 retry すべきでない

empty stop frequency 測定 (= 50% probabilistic) と組み合わせて Option F (= observe-only) に redirect。 これは Reyn の design philosophy の **核心** に触れる介入だった:

- LLM の確率的 glitch は **LLM の問題**
- Reyn は **observability で surfacing**、 user に判断を委ねる
- auto-rescue は P3 違反気味、 OS bloat trap

教訓: **「LLM が失敗したら Reyn が直してあげる」 は予測可能性原則違反**。 user に visible にして user 判断に委ねるのが正しい。

### TP3: user 「過剰ケア禁止 = 全くケアするなではない」

care boundary を言語化した時の補足:

> 過剰ケア禁止と言ったのは常にケアするなではないよ。 全くケアしなかったら reyn そもそも機能しないのでね。 このバランス感覚がまだ言語化できてないけど。。

これで 3 区分 framework (= structural / behavioral / gray) が言語化された:
- structural: pre-call 環境整備 ✅ Reyn care
- behavioral: post-call rescue ❌ Reyn care しない
- gray: prompt rule 累積 ⚠️ bloat trap 注意

care boundary concepts doc (= en + ja) + 5 つ目 feedback memory + CLAUDE.md cross-ref として永続化。 これは future の fix 設計時に「これは structural? behavioral?」 で迷わない framework。

教訓: **設計原則は dogfood 中の議論で言語化される**。 batch 7 の 3 つの user 介入が連鎖して 1 つの principle に収束したのが象徴的。

## 観測 infra のインパクト

整備された道具:

| ツール | 機能 | 価値 |
|---|---|---|
| `REYN_LLM_TRACE_DUMP=<path>` | LLM call の full payload を JSONL dump | 「LLM が何を見たか」 を初めて observe 可能に |
| `dogfood_trace.py llm-{payloads, detail, tools-schema}` | dump file の inspect | enum / system prompt / tools schema を分単位で確認 |
| `dogfood_trace.py llm-* --trace a,b,...` | multi-trace merge | cross-session 比較が容易に |
| `llm_replay.py <id> --trace <path>` | dump payload を litellm 直接 replay | reyn 起動なしで LLM 挙動 isolate 観測 |
| `llm_replay.py --patch '...'` | payload 改変 replay | fix 効果を **fix landing 前に** 検証可能 |
| `llm_replay.py --diff` | original vs replay 比較 | machine-readable 比較で iteration 高速化 |
| `llm_replay.py --n <count>` | N-shot 同 payload | 確率分布測定 (= deterministic vs probabilistic 判定) |
| `llm_replay.py --model <override>` | 別 model で replay | G4 spike (= 強モデル比較) を 1 LLM call で実行可能 |
| `detect_attractor.py` | attractor 自動検出 (3 heuristic) | empty stop / enum violation / hallucinate name の機械検出 |

これらが揃った後の **iteration speed** は劇的に変化:
- 道具なし: hypothesis → 仮説スタック → 推測 fix → 別 regression 露呈 → 再推測 (= 数日〜週単位)
- 道具あり: hypothesis → payload inspect (5 分) → `--patch` 実験 (5 分) → fix design (= 数時間)

batch 7 後半の wave (= router enum fix + Option F + B8-NEW-1+2) が **全部観測 evidence で動機付け** されているのは、 道具がなければ達成できなかった speed と confidence。

## 過去推測の retroactive 検証で訂正された事項

道具整備後に過去推測 4 件を verify、 1.5 件が **観測で訂正**:

| 過去推測 | 観測 verdict |
|---|---|
| 「過剰 consolidation で MUST rule 削れた」 | **Refuted**: rule は injected 済 |
| 「H3 観測不能は B7-NEW-1 が blocker」 | **Partially wrong**: 真の blocker は B7-S5b |

これらは fix 設計に直接 影響していた可能性。 道具なしで進めていたら:
- 「rule を削らない consolidation」 で fix 試みる → 効果ない (= rule 元々消えてない)
- B7-NEW-1 fix が H3 を unblock すると思って待つ → 実際は B7-S5b が真の blocker、 待ってても解消しない

= 推測は推測 stack に乗っかって自己強化、 確認しないと fix が空振りする。 観測こそが推測スタック解体の唯一手段。

## prediction 設計の教訓

batch 7 で試行した分布形式 prediction (= verified / inconclusive / refuted の 3 区分) は意図通り calibration data を産んだが、 **`blocked` カテゴリを含めていなかった** ため top probability category mass miss が目立った:

| Scenario | Top prediction | Actual verdict | Hit/Miss |
|---|---|---|---|
| S1 | 60% verified | blocked | miss (新 bug 露呈) |
| S5a | 55% verified | refuted | miss |
| S5b | 80% verified | refuted | miss |

教訓: **dogfood prediction は 4 区分 (verified / inconclusive / refuted / blocked) で組むべき**。 「前段で blocked」 は dogfood で頻出する outcome、 prediction に含めないと calibration が歪む。

## チームダイナミクス (= user vs assistant)

| TP | user 介入の質 | 私の応答 | 学習 |
|---|---|---|---|
| TP1 (推測指摘) | 観測道具の不在を突く methodological challenge | 軌道修正 + 観測 infra 整備 dispatch | 推測 → 道具 → 観測 の cycle を初めて完備 |
| TP2 (過剰ケア指摘) | design philosophy の核心を articulate | Option B 却下 + Option F へ redirect | care boundary の片半分 (= behavioral observe-only) を発見 |
| TP3 (バランス補足) | 「全くケアしないも違う」 で gray zone も articulate | 3 区分 framework に収束、 doc 化 | care boundary 完成 |

= user の介入は逐次「私が見落としている設計次元」 を補完する形。 これは user の **「言語化できてない」 と認めた感覚から生まれた principle** で、 ある意味 dogfood が user の 暗黙知を visible にした。

## 次 batch (= batch 8) への申し送り

### prediction 設計
- 4 区分 (verified / inconclusive / refuted / blocked) に拡張

### 観測道具の運用
- `REYN_LLM_TRACE_DUMP` を **default で on にする** option も検討余地あり (= dogfood は debug が前提)
- multi-trace merge を活用して同 input × 複数 fix state の cross 比較

### 残課題
- empty stop の **context 要因診断** (= `--patch` で system prompt / messages 改変 → rate 探り) は long-term root cause investigation として継続
- describe_skill 強制 (= H2 第 2 層 input field hallucinate)
- batch 8 で 累積 fix 全部の e2e effect 確認 (= 別 session で fresh dogfood)

### 設計原則の運用
- care boundary 3 区分 framework を **fix 設計時の checklist** として運用
- 「これは structural? behavioral? gray?」 を 1 質問目に置く

## 一言で

> **「LLM がおかしい」 と疑う前に、 LLM に渡したものを観測する道具を作れ**

— 道具なしの推測 = 推測スタック自己強化トラップ
— 道具あれば推測 1 件 5 分で確定 / 訂正可能
— Reyn の責務は LLM 環境整備と observability、 LLM 結果の auto-rescue でない

batch 7 の core narrative。
