# Batch 4 (retest + nested chain) — Retrospective

> B3-H1 fix は specialist の invoke 到達まで届いた。 ただし「最後の 1 cm」
> で reply が user に届かない (B4-H1) という、 attractor を抜けた先で OS の
> outbox routing layer に新しい穴が現れた話。 nested skill chain は 3 layer
> で動いたが、 LLM-driven な copy step (= copy_to_work) で全部崩れた。

---

## 前提 — batch 4 の起点

batch 3 で B3-H1 (specialist list→stop) を `48676ad` で fix、 B3-M2 を
`d6a987b` で root cause 解消、 B3-M1 を `d8328b2` で scenarios.md 修正。
713 passed。 **B3 fix の retest** + **nested skill chain 初観測** が batch 4 の
2 軸。

batch 3 retrospective で言語化した「fix の effectiveness は internal metric と
user metric の両方で測る」 が batch 4 の主題に直結する。

事前 prediction (3 件):

| Scenario | 予想 | 現実 | 結果 |
|---|---|---|---|
| S1 retest (B3-H1+H2 後の curry) | 70% で届く。 外れ予測 = chain 接続新問題 | invoke 到達 ✅ ただし narrator reply が agent_replies に届かず | 外れ (= 「chain 接続」 を outbox routing layer の問題として的中) |
| S2 retest (B3-M2 後の ask_user) | 40%。 外れ予測 = MCP catalog timing 等 | trial A は list_skills 経由するも catalog 不在、 trial B は direct reply | 当たり (= partial、 LLM 分散あり) |
| S3 nested skill (skill_improver) | 35%。 外れ予測 = run_skill 不使用設計 | 3 layer 観測 ✅ ただし copy_to_work で cascade 失敗 | 当たり (= chain 起動、 ただし新 attractor B4-H2) |

精度 1/3 (= S2 のみ単純 hit、 S1 / S3 は partial)。 batch 3 の 4/5 から低下
したように見えるが、 これは「fix 後の動作確認」 batch の特性で prediction が
high (70%) 寄りに振れていたのが大きい。

---

## main 発見 — narrative

### B4-H1: 「最後の 1 cm」、 invoke した結果が user に届かない

S1 retest で B3-H1 fix の効果は確認できた:

- specialist が `list_skills` → `invoke_skill("direct_llm")` まで到達 ✅
- direct_llm が curry recipe を生成 ✅
- skill_narrator が reply text を生成 ✅
- ただし **user には届かない**

deep dive で root cause 判明: `_run_skill_awaitable()` が narrator 結果を
**private `_put_outbox`** で送出していた。 これは default の outbox には
入るが、 RouterLoop が監視している `_router_loop_agent_replies` collection
には届かない。 結果 RouterLoop 終了時に `agent_replies` が空、 default は
B2-H2 fix path で「specialist から処理結果が得られませんでした」 を user に
送る (= **silent absorption fix の誤検知**)。

つまり B2-H2 fix が、 batch 3 で塞いだ attractor を抜けた経路に対して
誤発火する状態になっていた。 **「fix したい挙動 (silent absorption)」 と
「fix してはいけない挙動 (正常完走の skill output)」 の boundary が outbox
routing layer の API 設計で崩れていた**。

修正 `ffc9b4a`: `_run_skill_awaitable` 内側に 2 行 guard 追加、 narrator reply
を `_router_loop_agent_replies` にも append するよう変更。 `_append_history`
の二重呼び出しは構造的に起きない (= 既存 code path の対称性で保証) ことを
Tier 2b 2 件で pin。

### B4-H2: copy_to_work、 act budget の中で写経すらできない

S3 で skill_improver chain を初観測。 階層は 3 layer:

```
L1: skill_improver (run_id=...)
  L2a: eval_builder      (prepare phase で起動)
  L2b: eval              (run_and_eval phase で起動)
    L3: direct_llm       (eval.run_target で起動 — エラーで終了)
```

期待は 3-4 layer。 `judge_phase` を 4 layer 目と仮定していたが、 これは
`eval.evaluate` の **iterate preprocessor** として走り、 `run_skill` IR op の
階層には入らない (= P1-P8 整合、 phase 内 hook と phase 境界の区別)。
予測値の見積もり方を 1 件学んだ。

ただし eval cascade は **score 0.0 で全崩壊**。 root cause は `copy_to_work`
skill phase の `max_act_turns: 3` 不足。 LLM が glob で全 stdlib (39 skill) を
引っ張り、 read で 2 turn 消費し、 write 到達せずに force finish。 結果
`.reyn/skill_improver_work/direct_llm/` が作成されず、 `eval.run_target` が
`[Errno 2] No such file or directory` で失敗。 fail を受けた eval が score 0.0、
改善案は出ず、 skill_improver chain 全体が無効化。

修正 (`d9787cb`): `max_act_turns: 3 → 6` + glob pattern instruction で
`<original_dsl_root>` prefix 強制。 これは「budget 拡大」 + 「instruction
強化」 の典型的な **延命策** で、 batch 5 で同 LLM が再び glob constraint 違反
+ write skip を再現することになる (= G2 の真の解 = preprocessor 化への
道筋)。

### B4-INFO-A: 階層深度を予測する難しさ

`eval_builder` が `run_skill` 不使用と判明したのは batch 3 S3。 `skill_improver`
を入り口にすれば最深 4 layer と仮定して batch 4 S3 で観測したところ、
実際は 3 layer。 `judge_phase` を「もう 1 階層」 と数えるかどうかで
観測値が変わる。 これは「**階層の counting 単位を P1-P8 で明示しないと
予測が崩れる**」 という設計教訓。

run_skill IR op = phase 境界 (= 階層 count に入る)、 preprocessor / postprocessor
= phase 内 hook (= 階層 count に入らない)、 と区別を docs に記載すべき
(= 別 wave で扱う doc fix)。

### B4-INFO-B: parent_run_id 欠如、 階層は co-location でしか復元できない

3 layer 観測中、 `run_skill_started` event に `parent_run_id` field が無く、
階層関係は `events/chat/*.jsonl` 内の **時系列 co-location** でしか復元
できないと判明。 `/skill list` slash で「parent / child」 形式に表示する
要件は plan file の R-D13 として既に存在し、 batch 4 観測がその motivation
evidence になった。

---

## 感覚との差

batch 4 でも A4 step skip。 retrospective でも「user 感覚」 を事後言語化:

### 「invoke は到達したけど reply が届かない」 user 体験

S1 retest で specialist が裏で完璧に動いた (= invoke 到達 / レシピ生成 /
narrator 完了) が user には「specialist から処理結果が得られませんでした」 が
届く。 user 視点では:

- 何が起きてるか **見えない** (= 内部 invoke は WAL 観察しないと不明)
- 「specialist 壊れてる?」 と思う、 もしくは「reyn 壊れてる?」 と思う
- B2-H2 fix で error message は出るが、 「**動かないこと**」 は伝わるが
  「**裏で何が動いたか**」 は伝わらない

→ B4-H1 fix で routing 解消したが、 「何が動いたか」 を user に見せる仕組み
(= 進行 indicator / 部分結果 streaming / event timeline view) は別軸の
UX 課題として残る。

### skill_improver の cascade 失敗を user はどう受け取るか

S3 で skill_improver が改善案を出さず終わる。 user 視点では:

- 「improvement 出来なかった」 と分かるか?
  → narrator が score=0.0 を summary で伝える (= 部分的に伝わる)
- 「なぜ score 0.0 か」 は分かるか?
  → 「target skill not found」 のような技術 error は user には不可解
- 「next action は何か」 は分かるか?
  → 何も提示されない、 user は再 invoke するか諦めるかの判断が必要

→ skill_improver は production 的な workflow なので、 失敗時の
「actionable error message」 の整備が別 wave で必要。 batch 6 以降の検討項目。

---

## process 評価

### sequential vs parallel の境界線

batch 4 で fix 3 件 (B4-H1 / B4-H2 / B4-L1) を sonnet 並列で landing。
fix 同士の file overlap は最小 (= session.py / copy_to_work.md / 別 instruction)
だったので衝突なし。 ただし retrospective でも触れる通り、 並列で fix と
retest を回した結果、 batch 5 で **fix landing 前の HEAD で retest が走る**
タイミング不整合を batch 5 retest 2 で観測 (= G2 fix 前に B5R2-H2 が走った
事象)。

batch 5 retrospective で「並列化は fix wave で OK、 検証 wave は sequential
+ fix landing 後 dispatch」 をルール化することになる。

### dogfood_trace tool が batch 4 で活躍

batch 4 中に dogfood_trace.py を建てたわけではないが、 batch 4 後の batch 5
retest 2 でこの tool を必須化した。 batch 4 で各 sonnet が手動で 8-12 個の
grep を打っていた cost が、 batch 5 retest 2 では 1 コマンドに圧縮。
operational efficiency が batch を重ねるほど改善する pattern が確立した。

---

## 次回への持ち越し (batch 5 で扱った)

- B4-H1 fix の e2e effectiveness (= curry 届くか) → batch 5 fix-verify で
  prereq blocked、 batch 5 retest 2 で narrator reply 経路 ✅ 確認
- B4-H2 fix (copy_to_work budget) は workspace 作成成功確認 → batch 5 で
  再び write skip → 真の解 G2 (preprocessor 化) で `763c86c` resolved
- B5-M1 (parallel skill_improver invocation) を batch 5 で発見

---

## 教訓

1. **「最後の 1 cm」 問題**: attractor を抜けた先で OS の routing / outbox /
   state 管理 layer に別の穴がある。 prompt fix (= LLM 判断側) で解決できる
   問題と OS routing fix で解決すべき問題は **明示的に区別する**
2. **延命策と根治の境界線**: B4-H2 の budget 拡大 + glob instruction 強化は
   「LLM の attractor を抑え込もうとする勝てない戦い」 だった。 真の解は
   G2 = LLM を排除する preprocessor 化 (= memory `feedback_deterministic_split.md`
   で formalize)
3. **階層の counting 単位を明示せよ**: nested skill chain の predict は
   phase 境界 (= run_skill IR op) と phase 内 hook (= preprocessor) の区別
   なしに数えると外れる。 docs / ADR に明記すべき
4. **parent_run_id 欠如は R-D13 motivation**: dogfood 観測で「co-location
   依存の階層復元」 の脆さが evidence 化、 plan file の R-D13 が単なる
   nice-to-have から must-have に格上げ
5. **fix 並列 + retest 並列は危険**: HEAD 整合性が崩れる、 batch 5 retest 2 で
   B5R2-H2 が pre-G2 HEAD で走った教訓。 batch 5 retrospective で運用ルール
   として明文化
