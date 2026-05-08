# Batch 3 (ask-user-and-nested) — Retrospective

> B2-H2/H3 fix は機能、 H1 は variant attractor (`list_skills → stop`) で再発。
> B2-M4 (narrator) は触らずに自然改善で resolved。 ask_user e2e は依然 dark。
> attractor は塞いだ穴の脇から漏れる、 と言うべきか。

---

## 前提 — batch 3 の起点と事前 prediction

batch 2 が 5 scenario で 11 件 finding (HIGH 3 / MED 4 / LOW 3 / INFO 1) を
出し、 HIGH 3 件全て fix で landing した。 Tier 2 は 666 passed。 batch 3 は
**「fix した HIGH の effectiveness を user 視点で再確認 + 新規領域を観測」** が
目的。 重点 5 軸:

- multi-agent re-confirm (= curry recipe 届くか、 B2-H1+H2 fix 後)
- ask_user e2e (= B2-INFO で dark、 IR op を強制発火させる scenario 設計)
- nested skill (= run_skill IR op 初観測)
- narrator 品質 (= B2-M4 の 2-turn 体験継続か)
- skill 名 hallucination (= B2-M1 改善確認)

事前 prediction (5 件、 batch 2 retro より外れ予測も意識的に併記):

| Scenario | 予想 | 現実 | 結果 |
|---|---|---|---|
| S1 (multi-agent re-confirm) | 70% でカレー届く。 外れ予測 = chain 接続新問題 | specialist が `list_skills` 後に空 reply (= **新 attractor B3-H1**) → curry 未達 | 外れ (= chain 接続でなく attractor variant で外れた) |
| S2 (ask_user e2e) | 40%。 外れ予測 = router pre-skill clarification | router が tool 呼ばず direct reply (4 turn 全て) → IR op 未発火 | 当たり (= 「外れ予測通り」 で hit) |
| S3 (nested skill) | 35%。 外れ予測 = setup 問題 | `eval_builder` が `run_skill` 不使用設計と判明 → setup 不可 | 当たり (= 「外れ予測通り」 で hit) |
| S4 (narrator 品質) | 45% で 1 turn 成功 | 1 turn で具体的内容届く ✅ (B2-M4 自然改善) | 当たり |
| S5 (skill 名 hallucination) | 30% で消える | `general.summarize` 発明はなし ✅、 ただし list 後 invoke skip の別 attractor (B3-M3) | partial |

精度: **方向当たり 4/5**。 batch 2 の 3/5 から改善。 唯一 S1 のみ大外れ
だが、 これは「外れ予測 = chain 接続で新問題」 まで意識していたものの、
**attractor の variant 化** を予測しきれなかった。 「fix が効いた領域は別経路で
再発する」 という pattern を batch 4 以降の prediction で意識的に組み込むこと。

---

## main 発見 — narrative

### B3-H1: F3 の亡霊、 今度は list_skills の手前で止まる

batch 2 で B2-H1 (= `describe_skill` 後の停止) を `83bad83` で塞いだ。
具体的には `router_system_prompt.py` に「`describe_skill` 後は `invoke_skill`
か明示的説明」 という commit obligation rule を追加した。 batch 3 S1 で
同じ scenario を回すと、 attractor は **1 段階手前** に移った:

```
list_skills("") → list_skills("general") → 終了 (空 reply)
```

`describe_skill` を経由せず、 `list_skills` の結果を見ただけで「自分は
何もしない」 と判断する patte が出現した。 B2-H1 fix は describe → 何か
の遷移を縛ったが、 list → describe の遷移は野放しだった。

これは「attractor は同じ意図解釈の変種で再発する」 という generalization の
最初の test case。 fix は B2-H1 と同型の rule (`list_skills` 後 commit
obligation) を 1 行 1 bullet で追加して `48676ad`。 ただしこれは後の batch 5 で
過剰 rule 追加の代償 (= consolidation regression、 G1) を生むことになる。

### B3-M2: 「`read_local_files` を使って」 と言ってるのに使わない

S2 で skill 名を明示してすら、 router が tool を呼ばず direct reply を
返す挙動が 4 turn 全てで観測された。 当初は「ask_user 観測のための path 曖昧
誘発」 が目的だったが、 そもそも skill が起動しないので IR op まで到達せず、
ask_user 観測機会が消えた。

root cause investigation (`908b21e`) で原因判明: `_list_skills(path)` は
`path` 引数を **category** として扱う設計だったが、 LLM は `path` を
**skill name** として渡してくる (= 「read_local_files を呼ぼう」 → `path=read_local_files`)。
category と skill name の名前空間衝突で空配列が返り、 LLM は「skill 不在」
と誤判断、 direct reply に逃げる。

fix は `_list_skills` に **name lookup fallback** を追加 (`d6a987b`、 9 行
+ 3 Tier 2 test)。 prompt 触らずに code-side で解消。 これは「**prompt rule
追加で fix 可能だが、 root cause が code design なら code 側で直す**」 という
G6 (= router intent confidence gating の give-up カテゴリ) の対極にある healthy
fix path。

### B3-INFO-A: nested skill の前提が崩れた

S3 は `eval_builder` を入り口に nested run_skill chain を初観測する目的
だったが、 事前確認の段階で `eval_builder` が `analyze_skill → write_eval`
の 2 phase で完結 (= `run_skill` IR op を一切発行しない) 設計と判明。 scenario
は実行せずに closed。

代わりに stdlib 内で `run_skill` を使う skill を identify:

- `eval` の `run_target` phase (= target skill を呼ぶ)
- `skill_improver` の `run_and_eval` phase (= `eval` + `eval_builder` を呼ぶ)
- `judge_phase` (= eval から呼ばれる被呼び出し側)

最深 4 階層 nested を観測したいなら `skill_improver` を入り口にすべき。
これは batch 4 (= B4-S3) で実証され、 実際の階層は **3 layer** と判明
(judge_phase は preprocessor で run_skill 層ではない)。 「予測した深さは
予測通りに観測されない」 ことが学べた。

### B2-M4 → B3-INFO-B: 触らずに直る fix もある

S4 で B2-M4 (= narrator が「完了しました」 のみ) を再現確認したところ、
1 turn 目で具体的に「Reyn プロジェクトは予測可能性、 監査可能性、 制約優先
の LLM ワークフロー OS と説明されている」 という README.md 内容を含む reply
が届いた。 batch 2 観測時の 2-turn 体験は再現せず。

これは **LLM 出力のばらつき範囲だった可能性**、 もしくは **batch 2 観測時の
prompt context が偶発的に narrator を generic に振っていた可能性**。 触らない
fix で resolved に変えた稀少例。 dogfood は「再現する事象」 と「ばらつく事象」
の区別を強制する仕組みでもある。

---

## 感覚との差

batch 3 では A4 step (= user 感覚 review) を skip して fix wave を即実行
した運用を取ったため、 「assistant の test 観点」 と「user の体験観点」 の
言語化機会が薄くなった。 この retrospective で事後的に補完する:

### S1 を「失敗」 と判定したか

assistant 視点では「specialist は invoke_skill 呼ばなかった = 失敗」 で
B3-H1 finding 化した。 user 視点で同じ挙動を見たらどう感じるか:

- 「specialist にカレー聞いたのに何も返らない、 沈黙される」 = 完全に **使えない** 体験
- 内部的に `list_skills` が呼ばれていたかどうかは user に見えない
- B2-H2 fix (peer_reply_failed_surfaced) で「specialist から処理結果が
  得られませんでした」 が default 経由で届くため、 沈黙ではなく「使えない」
  ことが伝わる UX に**辛うじてなっている**

→ assistant の HIGH 判定は妥当。 ただし「invoke_skill 到達」 を success
metric にすると user 視点の「届く」 と乖離する。 batch 4 retest で B3-H1 fix
後に invoke_skill 到達したが curry が user に届かなかった (= B4-H1) という
事象がこの乖離を象徴。

### 「fix した」 と「使えるようになった」 のギャップ

batch 3 で HIGH 1 + MED 2 を fix したと記録したが、 batch 4 retest で:
- B3-H1 fix の効果は「invoke_skill 到達」 まで (= 内部 metric)、 user 視点の
  「curry 届く」 は B4-H1 で別 layer に阻まれていた
- B3-M2 fix の効果は「list_skills が name で hit」 まで、 user 視点の「ask_user
  prompt が見える」 までは別 wave で要 verify

→ **fix の effectiveness は internal metric と user metric の両方で測る** べき。
batch 6 以降の運用ルールに追加。

---

## process 評価 (A1-A5 + 並列化)

### A1 (scenario plan)

scenario の事前 prediction に「外れ予測 = どんな失敗パターンが想定されるか」
を併記する形を batch 3 から確立した (= scenarios.md に各 scenario の「外れ
予測として意識する点」 を 1 段落)。 これは batch 2 までの 0 行から大きな
進化で、 prediction 精度の self-evaluation を可能にした。

### A3 (実行)

5 scenario を sonnet sub-agent × 5 並列で worktree 隔離で実行。 cost 効率は
高かったが、 各 sonnet の output (= per-scenario observation file) が
**raw 観測の貼り付け** に近く、 「事件として書き残す」 quality が batch 1-2 と
比較して低下した。 これは batch 4-5 まで続く問題で、 retrospective 補完
(= 本 doc) の動機になった。

### A4 (user 感覚 review): 完全に skip した

batch 1-2 では A4 で user feedback を memo に反映してから A5 fix wave を
回した。 batch 3 ではこの step を完全に skip し、 assistant が finding 起こし
→ 即 fix wave dispatch に繋いだ。 結果:

- speed は出た (= 5 scenario / fix 4 件 / 全 commit を 1 day 内に landing)
- ただし「assistant 観点と user 観点のギャップ」 が言語化されないまま
  batch 4 / 5 に持ち越され、 cross-batch interference (= B5-H1 consolidation
  regression、 user feedback で初めて user 視点の「過剰 consolidation も
  bloat と同等の問題」 が言語化された) を起こした

→ batch 6 以降は A4 step を運用に戻す。 fix wave 着手前に user に findings
を見せて感覚 review を必ず待つ。

### A5 (fix wave)

3 件の HIGH+MED fix (B3-H1 + B3-M1 + B3-M3 を 1 commit、 B3-M2 を別 commit)
を sonnet 並列で landing。 fix 自体は機能したが、 上記 A4 skip が後の
B5-H1 regression を生んだ。 「並列化は fix wave で OK、 検証 wave (= retest)
は sequential」 というルールを batch 5 retrospective で明文化することに
なる。

---

## 次回への持ち越し

### batch 4 で扱った
- B3-H1 / B3-M3 fix の effectiveness 再確認 (= curry 届くか) → B4-H1 で
  「invoke 到達したが届かない」 別 attractor 発見
- nested skill chain の初観測 (= skill_improver 経由) → B4-H2 / B4-L1 発見

### batch 5 / 5 retest 2 で扱った
- B3-M2 fix 効果 (= S2 retest)
- B5-H1 (= consolidation regression、 batch 5 で初めて surface)

### batch 6+ に持ち越し
- ask_user IR op 観測 (= G5)、 強モデル trial 込み
- B5R2-H1 (describe_skill→stop attractor) — prompt 強化路線でなく code-side gate
- non-giveup 残件: B3-L1 / B3-L2 (LOW polish)

---

## 教訓

1. **attractor は塞いだ穴の脇から漏れる**: B2-H1 fix (`describe → 停止`) を
   塞ぐと list → 停止が現れ、 それを塞ぐと describe → 停止が batch 5 retest 2
   で再発した。 1 つの「commit obligation」 で全 chain を gate するよりも、
   状態遷移を OS 層で gate する設計が必要 (= batch 6 以降の構造的検討項目)
2. **prompt rule 追加は line 単位で見れば minimum、 累積で見れば bloat**:
   batch 1 の F3+F9 → batch 2 の B2-H1 → batch 3 の B3-H1+M3 と 3 batch で
   3 rule 追加した結果、 batch 5 で過剰 consolidation regression を生んだ。
   memory `feedback_prompt_design.md` で formalize 済
3. **「触らないで直る」 fix もある**: B2-M4 が batch 3 で自然改善。
   全 finding に対し fix を急ぐのでなく、 再現性確認を retrospective に組み込む
4. **prediction の「外れ予測」 を書いてから実行**: batch 3 で確立したこの
   format が batch 4-5 でも当たり率の self-evaluation を可能にした
5. **A4 step skip は技術 debt**: speed は出るが cross-batch interference を
   生む、 batch 6 以降は必須運用に戻す
