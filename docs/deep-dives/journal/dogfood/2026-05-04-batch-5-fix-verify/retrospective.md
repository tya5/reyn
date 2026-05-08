# Batch 5 (fix-verify + retest 2) — Retrospective

> 過剰 consolidation が signal 弱化を生み、 batch 4 で塞いだはずの attractor が
> 違う名前で蘇った話。 教訓は memory に書いた。 ただ、 user feedback (=
> 「肥大化、 重複、 過剰適合」) が consolidation 過剰の方向へ私を押し、 結果
> regression を生んだ責任は明確に私にある、 という事実も記しておく。

> 本 retro は **batch 5 fix-verify (= 2026-05-04-batch-5-fix-verify/)** と
> **batch 5 retest 2 (= 2026-05-04-batch-5-retest2/)** の 2 sub-batch を
> 統合的に振り返る (= 同日内に 2 回回した連続観測なので 1 retro でまとめる)。

---

## 前提 — batch 5 の起点

batch 4 で B4-H1 (`ffc9b4a`) + B4-H2 (`d9787cb`) + B4-L1 を landing。 加えて
**user feedback** 「肥大化、 重複、 過剰適合に気をつけましょう。 シナリオ間の
実行結果の相互影響を減らす方法の一つになるはずです」 を受け、 router system
prompt を 4 rule → 2 段落 に **consolidation refactor** (`e90c0f2`) を実施した。

事前 prediction (2 件):

| Scenario | 予想 | 現実 | 結果 |
|---|---|---|---|
| S1 (curry retest) | 90% で届く (B4-H1 fix 効果検証) | specialist が再び list_skills 後空 reply (= **B5-H1 regression**) → 未達 | 大外れ |
| S2 (skill_improver retest) | 90% で部分完走 | workspace 作成 ✅ ただし eval cascade で path 形式 mismatch (B5-H2) | partial 当たり |

精度 0/2 (実質)。 **prediction 90% が両方崩れた**。 fix-verify batch の
predicting bias (= 「fix したから動くはず」) が露骨に現れた事例。

---

## main 発見 — narrative

### B5-H1: 「user feedback の善意」 が regression を生んだ

時系列:

1. batch 1-3 で router system prompt に MUST rule を 4 件積み重ねた
   (F3+F9 / B2-H1 / B3-H1+M3)
2. **2026-05-04 中盤の user feedback**: 「肥大化、 重複、 過剰適合に気を
   つけましょう」 + memory `feedback_prompt_design.md` を整備
3. その feedback を額面通り受け、 4 rule を 2 段落に consolidation refactor
   (`e90c0f2`)。 LOC -33%、 MUST count 3 → 1
4. batch 5 fix-verify S1 で specialist が `list_skills → list_skills → 空 reply`
   再現。 batch 3 で塞いだはずの attractor が「違う名前で蘇った」

root cause: weak LLM (gemini-2.5-flash-lite) は **paragraph 内の複数 sentence 形式の
MUST を 1 段落 = 1 priority signal** として扱い、 各 sentence の MUST が
個別に honor されない。 個別 bullet × 各 1 MUST × 複数 = priority 信号 N 個、
段落 × N sentence = priority 信号 1 個、 という非対称が weak LLM 固有の特性。

修正 (`ca116f3`、 partial revert): 4 rule → 5 個別 bullet × 各 1 MUST、 wording
は dedup したが構造は分離維持。 LLMReplay fixture 7 entry rekey。

教訓 memory に追記 (`feedback_prompt_design.md`):

> 過剰 consolidation も regression を生む。 weak LLM への optimal balance は
> **個別 bullet × 1 MUST × wording dedup**。

これは **user feedback を額面通り適用する危険** の事例。 「肥大化を避ける」 が
正解だが、 「consolidation 過剰」 という別の極に振れた。 user feedback を
「方向感」 として受け、 「具体的 implement で over-correct しない」 判断が
必要だった。

### B5-H2: 同じ error message、 異なる root cause

S2 で eval cascade が `control_ir_failed: {"kind": "run_skill", "error": "'name'"}`
で全 score 0.0。 当初は「`run_skill` IR op handler が `name` field を期待」 と
解釈し、 `eval.run_target` の instruction を「`skill` field 強制」 に修正
(`fe91321`)。

batch 5 retest 2 で再 verify したところ、 instruction fix は機能 ✅ ( = run_target
が `skill:` を出力)、 ただし **同じ error message** が persist。 deeper investigation
で別 root cause 判明: **`copy_to_work` が source content を read しているが
write op で content を omit、 0-byte file を生成**。 後続 `parse_skill` が空
frontmatter で fail し、 `KeyError: 'name'` を raise。

→ **「同じ error message を 2 つの root cause が共有」** していた。 fe91321 fix
で半分解消、 残り半分は B4-H2 fix (= 既に B4-H2 として認識していた write skip
attractor) と同根、 G2 preprocessor 化 (`763c86c`) で構造的解消見込み。

教訓: error message の specificity を上げる (= `'name'` だけでなく context
含む) と root cause 分離が早まる。 これは別 wave での observability 改善
候補として記録。

### B5R2-H1: describe_skill→stop、 attractor の三度目の variant

batch 5 retest 2 で B5-H1 fix の効果検証中、 specialist が:

```
list_skills("") → list_skills("general") → describe_skill("direct_llm") → 終了
```

`describe_skill` 後に `invoke_skill` を呼ばず exit する **新 variant** が出現
した。 これは B2-H1 (= batch 2 で発見した describe→stop attractor、 当時
`83bad83` で fix) が、 B3-H1 / B5-H1 fix の rule 修正過程で再発したもの。

attractor の歴史:
- batch 2 B2-H1: `describe → stop` (= 83bad83 で fix)
- batch 3 B3-H1: `list → stop` (= 48676ad で fix、 別 attractor)
- batch 5 B5-H1: B3-H1 が consolidation で破壊 (= ca116f3 で revert)
- batch 5 retest 2 B5R2-H1: B2-H1 と同じ位置で **再発** (= 5 bullet 形式の
  re-balance でも describe→stop の MUST bullet が weak LLM に届かなかった)

これで **「attractor は 1 つの変種を塞いでも別の段階で出現する」** という
generalization が確定。 prompt rule を bullet 単位で増やす戦略は **対応 lag**
を生み続ける。 構造的解は OS 層で「discovery 後の状態遷移を gate」 する設計
(= G6 と類似だが、 confidence でなく **state machine** ベース)。

具体策案:

- option A: `list_skills` / `describe_skill` 呼び出し後に `_router_loop_state` に
  `discovered_skills: list[str]` を保持、 次の LLM call で「discovered ≥ 1 かつ
  invoke_skill 未呼び」 なら強制 prompt injection で「invoke or explain」 を
  inline で push
- option B: `list_skills` / `describe_skill` の後に LLM が text reply のみで exit
  しようとしたら、 RouterLoop が `agent_message_sent` を delay して「discovered
  skills を活用しましたか?」 を 1 回 prompt し直す
- option C: prompt に rule を追加せず、 weak LLM 路線を諦めて strong model
  併用 (= G4 trigger 発火)

batch 6 設計時に検討。

---

## 感覚との差

### user feedback と implement のループ

user feedback は「方向感」 として優れていたが、 私が「具体的 implement」 で
過剰に振った結果、 batch 5 で逆方向の regression を生んだ。 これは
「**feedback の方向 vs 過剰適用の境界線**」 を私が事前に判断できなかった
ことが本質的問題。

正しい運用:
1. user feedback を **memory に formalize** (= ✅ 実施した)
2. ただし memory を読んで具体的 implement に落とすときは **「最小変更で
   方向に従う」** を優先する (= 4 rule → 2 段落 という極端な consolidation で
   なく、 4 rule × wording dedup のような穏当な変更)
3. dogfood で feedback の効果を verify してから次 wave に進む

batch 5 では (3) の verify を fix wave 後に置いた結果、 verify そのものが
regression を発見する形になった。 順序が「feedback → 実装 → verify」 でなく
「feedback → 実装 + 期待 → verify で発見」 になっていた。 batch 6 では
**feedback 由来の refactor は単独 PR で landing し、 必ず dogfood verify を
1 回挟んでから次の fix wave に乗せる** ルールを徹底する。

### 「予測 90% で外す」 の意味

batch 5 prediction が両方 90% で外れた。 これは「fix したから動くはず」 と
いう自信過剰 prediction の bias。 batch 4 retro でも触れた「fix の effectiveness
は internal metric と user metric の両方で測る」 を batch 5 で守れなかった
(= invoke_skill 到達を success と見なし、 user 体感の curry 届くを軽視した)。

batch 6 以降の prediction では:
- internal metric (= invoke 到達 / phase 完了 / event 発火) と
- user metric (= 内容届く / 失敗が actionable に伝わる / 体感 OK) を
- **分けて prediction**、 両方の hit/miss を記録する

---

## process 評価

### parallel sonnet が narrative quality を侵食した

batch 3 から続く問題が batch 5 で頂点に達した。 5 sonnet 並列で fix verify
を回した結果、 各 sonnet が「観測 raw を貼る」 だけで、 「事件として書き残す」
quality が崩壊。 user feedback 「**docs dogfood findings は batch 3, 4, 5 に
なるにつれて内容薄くなってきたね。 batch 1 作成時にどのような意図でこの
ドキュメント作ろうとしてたか覚えてる?**」 がこれを直接指摘した。

この feedback を受けて、 batch 5 retest 2 完了後に sequential catch-up wave
(= 本 retro の作成、 各 finding の 5 要素補完、 narrative 復元) を実施。

### 「キリのいいところで止まる」 運用の確立

batch 5 retest 2 中盤に user 「**なるほど、 並列が邪魔してるのね。 であれば
キリの良いところまで待ちましょう**」 で wind-down モード入り。 active sonnet
2 件 (B5 retest 2 + G2 preprocessor) の完了を待ってから新 dispatch を停止。
これは「並列度の上限管理」 を具体的に運用した最初のケースで、 batch 6 以降の
**autonomous mode 許可ベース** + **キリのいいところで止まる** ルールに繋がる。

### dogfood_trace + rekey CLI の整備

batch 5 で `scripts/dogfood_trace.py` (= grep 集約、 sub-agent tool_use 8-12
削減) と `scripts/rekey_fixtures.py` (= G9 resolved、 25-30 min/round → 30 sec)
を建てた。 operational efficiency が batch を重ねる毎に蓄積されていく
仕組みが確立した。

---

## 教訓

1. **user feedback の方向 vs 過剰適用**: feedback は memory に formalize、
   ただし implement では「最小変更で方向に従う」 を優先。 過剰 consolidation も
   regression を生む (= memory `feedback_prompt_design.md` の 2026-05-04
   update section)
2. **error message specificity**: 「同じ error message を異なる root cause が
   共有」 する設計は debug efficiency を下げる。 error message に context
   field を含める設計を OS 全体で見直すべき (= 別 wave 候補)
3. **attractor は 1 つの variant を塞いでも別位置で出現**: prompt rule を
   bullet 単位で増やす戦略は対応 lag を生む。 構造的解は OS 層 state machine
   での gate (= batch 6 検討項目)
4. **prediction を internal metric / user metric で分離**: 「invoke 到達」 を
   success と見なすと user 体感と乖離する。 両 metric の hit/miss を分けて
   記録する形を batch 6 で実装
5. **fix wave 後は単独 PR で 1 回 dogfood verify を挟む**: feedback 由来の
   refactor を fix 並列に乗せると regression 発見が遅れる。 sequential 化が
   必要
6. **「キリのいいところで止まる」 の運用化**: parallel sonnet を打ち切る
   timing を user 介入で明示する文化が batch 5 で確立。 batch 6 以降は
   autonomous mode を許可ベースで運用
