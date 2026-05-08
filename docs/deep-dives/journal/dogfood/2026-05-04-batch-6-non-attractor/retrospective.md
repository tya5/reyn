# Batch 6 (non-attractor focus) — Retrospective

> 「attractor は触らない」 と決めてから走ったバッチで、 attractor が最も明確に
> 存在を証明した。 G12 の 4 連続再現が Wave 3 G4 trigger spike の優先度を
> 確定論的に決め、 B5-M1 の完全再現が G3 fix の正当性を裏付けた。 一方、
> B2-M2 と B4-M1 は再現せず — ただし「外れた」 のではなく、 観測対象が
> 「想定 root cause」 より手前の別 layer で詰まっていたという事実が明らかになった。

---

## 前提 — batch 6 の起点

batch 5 retest 2 で `describe→stop` attractor が 3 度目の発生を記録した後、
当初は OS 層 state machine (= PR-state-gate) での gate 実装を提案した。 user の
返しは端的だった:

> こういうのを想定してギブアップリスト作ってるんですが、ギブアップリストに
> 移動ではだめな案件?

この一言で撤回、 G12 化 + Wave 3 G4 trigger spike に pivot した。 batch 6 は
その方針の下で、 **attractor 系は fix しない、 監視記録のみ** を明示的に決め
て走った最初の batch となった。

batch 開始時の main HEAD: `0660bb2`、 736 passed。 batch 6 execution 中に Wave 4
fix として G3 (`9798372`) と G10 (`af16228`) が並走で landing、 batch 終了時は
`fd852e5`、 743 passed (+7)。

### 事前 prediction の hit/miss (internal / user 分離)

batch 5 retro の教訓として「internal metric と user metric を分離して prediction
する」 を採用した最初の batch。

| ID | Internal pred | 結果 | User pred | 結果 | 方向 |
|---|---|---|---|---|---|
| S1 (G2 retest) | 90% workspace 作成 | MISS (0-byte write 解消、 glob 0 matches) | 70% 改善案届く | MISS (上流 hallucination) | MISS |
| S2 (G5 ask_user) | 30% IR op 発火 | MISS (G12 で invoke 未到達) | 20% prompt 届く | MISS | MISS (外れ予測 c 完全的中) |
| S3 (B5-M1) | 50% 並列再現 | HIT (3 並列、 決定論的) | n/a | n/a | HIT (保守的すぎ) |
| S4 (B2-M2) | 70% tool_failed 経路 | MISS (LLM が text reply 直行) | 50% 英語 reply | MISS (日本語) | MISS |
| S5 (B4-M1) | 80% 4 回 failed read | MISS (hallucination で abort) | n/a | n/a | MISS |

**方向当たり 1.5/5** (= S2 完全的中 + S3 保守的 HIT 0.5)。 過去 batch との比較:

| Batch | 方向 hit | 備考 |
|---|---|---|
| batch 1 | — | prediction 前 |
| batch 2-4 | 3-4/5 | G12 / B2-H1 series が安定に出た |
| batch 5 | 0/2 | prediction 90% が両方崩れた (fix-verify bias) |
| **batch 6** | **1.5/5** | 低水準、 ただし理由が明確 |

---

## main 発見 — narrative

### G12 attractor の 4 連続再現 — 定量的確定

S2 で `read_local_files skill を使って` と入力した。 router が `list_skills` と
`describe_skill` を連続して呼び、 その後 `invoke_skill` を発行しないまま空 reply
で終了した。 tool sequence は 2 件、 LLM call は 3 回、 所要 10 秒、 user に届
いたのは空白だった。

これは batch 2 B2-H1 で `83bad83` が打った MUST rule を今も prompt に持ちなが
ら発生した。 B3-H1 で追加した別の MUST rule もある。 `ca116f3` の re-balance も
入っている。 それでも weak LLM (gemini-2.5-flash-lite) は `describe_skill` 後に
`invoke_skill` を呼ばない選択を取った。

4 連続再現の系譜:

| Batch | Variant | 当時の対処 |
|---|---|---|
| batch 2 (B2-H1) | `describe → stop` | `83bad83` MUST rule |
| batch 3 (B3-H1) | `list → stop` | `48676ad` MUST rule |
| batch 5 retest 2 (B5R2-H1) | `describe → stop` | G12 化 (着手 defer) |
| **batch 6 S2** | `describe → stop` | 記録のみ (G12 policy) |

**prompt rule 路線の限界が定量的に確定した**。 「MUST rule が有効な間隔」 と
「無効化されるまでの batch 数」 が data として積み重なった。 Wave 3 G4 trigger
spike — 強モデルで同一 scenario を回し、 attractor 発生率を計測する — の
動機は十分に揃った。

### B5-M1 並列 invoke の決定的再現 — G3 fix の正当性

S3 で `skill_improver を使って direct_llm を review して` と入力した。 router
の 1 LLM call から `invoke_skill(name="skill_improver")` が 155ms 以内に 3 件
発行された。 セッションを変えても同じだった (Run 1 / Run 2 ともに 3 並列)。

各 instance は独立して別の判断を出した。 1 件が `ask_user` で skill path を質問、
1 件が `reyn/local/my_app/` への hallucinated path で copy_to_work まで進行、
1 件が artifact validation 失敗でリトライした。 同じ input から 3 種の経路が
同時に走る。

batch 5 では同様の事象が 333k tokens / 51 LLM calls まで達していた。 G3 dedupe
(`9798372`、 batch 6 A3 並走中に landing) が正しい fix である証拠が、 再現観測
によって定量的に確認された。 ただし本 batch は fix 前 HEAD (`0660bb2` の
worktree) での観測だったため、 G3 post-fix retest は次 batch で必須となった。

### B2-M2 (英語 fallback) — 観測対象が想定より手前で止まった

S4 の設計は「意図的に不存在 skill 名を投入して `tool_failed` event を誘発し、
英語 fallback reply (B2-M2) を再現する」 というものだった。 `nonexistent_skill_xyz123`
を router に渡した。

router の LLM call 1 回、 tool call 0 件、 日本語で「そのスキルは存在しません」
と直接 reply して終了した。 `tool_failed` event は発火しなかった。 G10 fix
(`af16228`) の経路は通らなかった。

**B2-M2 の root cause は `tool_failed` path でなかった可能性** が浮上した。
LLM が「存在しない skill」 と判断した時点で tool call を選択せず text reply に
逃げるパターン — これは G12 の broad family に属する。 G10 fix は `tool_failed`
が発火した場合に deterministic i18n table 経由で日本語 reply を出す実装で、
方向として正しい。 ただし LLM が `invoke_skill` を呼ばない場合は fix の効果が
届かない。 effective scope が想定より狭いという新たな観測が生まれた。

### B4-M1 (eval.md path) — 前提条件が崩れた

S5 で `prepare` phase の path search 挙動を観測しようとした。 `direct_llm`
というスキル名を入力すると、 LLM は target を `my_app` と解釈し、
`reyn/local/my_app/eval.md` を 1 回試みて abort した。 B4-M1 で観測した「4 回の
failed read」 は再現しなかった。

target の解釈が誤っているため、 path search の問題を観測する以前にチェーンが
崩れた。 これは B4-M1 とは別の layer の問題 — prepare phase が stdlib skill
(= `src/reyn/stdlib/skills/direct_llm/`) を `reyn/local/<name>/` に補完する
instruction の欠落 (B6-S1-H1)。 B4-M1 fix の前提条件として B6-S1-H1 の
hallucination fix が先に必要だと判明した。

### G2 preprocessor の動作確認 — 構造は正しく、上流が誤った

S1 では G2 fix (`763c86c`) の e2e effectiveness を検証した。 preprocessor は
8 step を 0 LLM call で完走した。 python で path 計算・copy plan 構築・write ops
生成・validate を行い、 run_op で glob を 2 回、 iterate で 2 回という順序は
確認できた。

問題は glob の結果だった。 `prepare` phase が渡した target path が
`reyn/local/my_app/` (hallucination) だったため、 glob は 0 matches を返し、
`_validation.ok = false / files_written = 0` になった。 preprocessor 自体の
構造的正しさは確認できたが、 上流 (= `prepare` LLM) の誤りがすべての出力を
無効化した。

副次発見として B6-S1-M1 (MED) が生まれた。 `_validation.ok = false` にも
かかわらず後続の LLM が「copied」 と判断して `run_and_eval` に遷移した。
preprocessor の validation 結果が LLM context に注入されていない、 P3 (OS =
runtime engine) が gate すべき箇所で gate していないという設計問題。

---

## 感覚との差 — A4 user review の後

findings aggregation を完了して A4 review に入ったとき、 予想外の方向から
本質的な指摘が来た。

B6-S1-H1 の fix 候補として、 `prepare` instructions に「stdlib skill の場合は
`src/reyn/stdlib/skills/<name>/` を参照せよ」 という prompt rule を追記する案を
私は提示していた。 user の反応は:

> LLM に path を扱わせなければ良いのでは?

この一言で前の turn の提案を撤回した。 「LLM に path を決めさせるから
hallucination が起きる。 skill identifier (= skill の名前) と path は分離して、
OS が解決して渡すべきだ」 という指摘は、 prompt rule の積み重ねではなく
**設計の root cause への intervention** だった。

これが Wave 1 (= skill identifier 純化) として次の着手方向に pivot した経緯。
「path 補完 instruction を追加する」 という対症療法を書きかけていたところを、
user feedback が根本解に引き戻した。

同じ A4 review で OS validation gate (Wave 2 案) についても議論した。
`_validation.ok = false` 時に OS 側で abort を強制する実装は P3 整合に見えるが、
OS responsibility の拡張は慎重な判断が必要で、 user は「判断待ち」 とした。
user judgment を優先した観察型 defer — これも G12 化と同じ構造の判断だった。

prediction 精度 1.5/5 の解釈についても user に確認した。 「scenario 設計が
悪かったか」 という問いに対して、 「LLM の判断ばらつき範囲が予想より広い。
同じ input でも別 attractor / 別 root cause で fail する」 という認識が合意された。
batch 7 では分布形式 prediction — 「30% A / 30% B / 40% C のどれか」 という
記述 — に進化する候補として挙がった。

---

## process 評価

### A4 step の復活

batch 3-5 で A4 (= user 感覚 review) をスキップまたは形骸化させた結果、
cross-batch interference と narrative quality 低下を招いた。 batch 6 では A2 と
A4 を「skip 禁止」 として明示した。 A4 で B6-S1-H1 の fix 方向が覆ったという
事実がそのまま A4 の価値を証明した。

### Wave 4 fix 並走

batch 6 execution 中に G3 (`9798372`) と G10 (`af16228`) が landing した。
これは batch 6 の observation と独立に進行しており、 cross-contamination はなかった
(= batch 6 は fix 前 HEAD の worktree で走った)。 ただし tracker の状態管理と
batch の HEAD 記録が重要で、 「batch 6 での G3 再現観測は fix 前の動作」 という
事実を明示しておく必要があった。

### Wave 2 (OS validation gate) の保留

B6-S1-M1 の fix として OS 側で `validation.ok = false` を abort に繋げる案は
技術的に実装可能だが、 OS responsibility の拡張を伴う。 user が明示的に
「判断待ち」 とした。 P3 整合に見える改善でも、 アーキテクチャの責任境界を
動かす変更は合議の上で進める、 という姿勢を batch 6 で再確認した。

---

## dispatch wave 完走後の追跡 — B6-S1-M1 系と新規 infra fix 2 件

### dispatch wave 構成

batch 6 の S1-S5 観測 (A3) が完了した後、 A4 user review で B6-S1-H1 の fix
方向 (= OS が path を解決して渡す) と B6-S1-M1 の仮説 (a) 検証が次ステップ
として確定した。 以降は **3 wave の並列 → 逐次 dispatch** で進行した:

| Wave | 内容 | 体制 |
|---|---|---|
| Wave 1 | B6-S1-H1 fix (eval_builder OS path resolution) | sonnet ×1 |
| Wave 2 | B5-M2 fix (skill_improver decide-turn instruction) | sonnet ×1 |
| Wave 3 | B6-S1-M1 仮説 (a) Tier 3 LLMReplay test + dogfood retest | sonnet ×1 (sequential) |

Wave 1 + 2 は並列で landing した (`e6de782` + `0fd6d0b`)。 Wave 3 は
`e6de782` / `0fd6d0b` の両方を前提とした retest だったため、 Wave 1/2 完了後に
逐次で実施した。

### B6-S1-M1 仮説 (a) の 2 経路検証

仮説 (a) (= `_validation` → `validation` rename が LLM の context 認識を改善した)
を検証するために 2 つの経路を並べた結果が以下の通りだった:

| 経路 | 手法 | 判定 | コメント |
|---|---|---|---|
| Tier 3 LLMReplay | hand-crafted fixture で behavioral pin | **verified (間接的)** (`9763ecf`) | `data.validation.ok` に基づく分岐を test で pin、 regression guard として確立 |
| dogfood retest | 実 LLM を chat / run mode で e2e 実行 | **inconclusive** (`07e16ca`) | preprocessor が step 0/1 で先行 fail → LLM 呼ばれず観測不能 |

dogfood retest が inconclusive になった理由は仮説 (a) の問題でなく、 **インフラ
層の別 gap 2 件** だった。 これが今 batch の最も意外な副次成果となった。

### 新規 infra bug 2 件 — dogfood retest 中に発見、 同 session 内で fix landing

#### B6-INFRA-1: `reyn chat` での trusted python 未サポート

`copy_to_work` preprocessor の step 0 は `mode='trusted'` の python step。
`reyn chat` コマンドは `--allow-untrusted-python` フラグを持たず、
`PermissionResolver` を `trusted_python_allowed=False` 固定で生成する。
`reyn.yaml` に `python.trusted: allow` を設定しても runtime の hard-fail は
bypass されない — 設計上の gap。

→ `reyn chat --allow-untrusted-python` flag 追加で `reyn run` との symmetry を確保。
Commit `07ee851`、 +4 test。 当 session 内で fix landing。

#### B6-INFRA-2: `Workspace.glob_files()` の stdlib boundary 拒否

`compute_paths()` が返す stdlib skill の glob path は absolute path。
`Workspace.glob_files()` が `base_dir` 以外を境界外として `PermissionError` を
raise する。 `file.read: allow` config は `PermissionResolver` 経由だが、
`Workspace.glob_files()` の boundary check は別レイヤー — 二重 gate 構造の gap。

→ `PermissionResolver` consultation を boundary check に追加、 stdlib path への
explicit perm で opt-in できる設計に変更。 Commit `f666acb`、 +4 test。
当 session 内で fix landing。

### infra fix 2 件 landing 後の状態

| Commit | 内容 | test 増分 |
|---|---|---|
| `e6de782` | eval_builder D1+D2+D3a fix (preprocessor 経由 OS path resolution) | +8 |
| `0fd6d0b` | skill_improver decide-turn instructions strengthening (B5-M2 H1+H2+H3) | +4 |
| `9763ecf` | copy_to_work validation judgment Tier 3 LLMReplay test (B6-S1-M1 仮説 a) | +2 |
| `07e16ca` | B6-S1-M1 仮説 (a) dogfood retest doc | 0 |
| `07ee851` | reyn chat --allow-untrusted-python flag (infra fix #1) | +4 |
| `f666acb` | Workspace.glob_files() perm consultation (infra fix #2) | +4 |

合計 +22 test、 0 regression。 main HEAD: `f666acb`、 775 passed / 2 xfailed。

**「chat 経由で skill_improver が動く前提が揃った」** — これが wave 完走の
headline。 B6-S1-M1 仮説 (a) の dogfood 観測は次 batch (batch 7) の retest
課題として持ち越しとなる一方、 Tier 3 が regression guard として確立された。

---

## 次回への持ち越し

### Wave 1 着手内容 (= 次の即着手)

user 指摘 「LLM に path を扱わせない」 を受けて、 skill identifier (= 名前)
と path を分離する方向が決まった:

1. **B6-S1-H1 fix** — `prepare` が `target_skill_path` を自力で構築するのではなく、
   router/OS が解決した path を artifact field として受け取る設計に変更
2. **B6-S1-M1 fix** — preprocessor の `_validation` 結果を次 phase の LLM
   context に inject、 `ok = false` 時は OS が遷移を gate する

### batch 7 設計候補

- **G3 / G10 post-fix retest** — 本 batch は fix 前観測、 次 batch で fix 後の
  挙動を確認
- **B6-S1-H1 / B6-S1-M1 retest** — Wave 1 fix landing 後の e2e 確認
- **attractor mapping schema** — G12 section の variant tag を formalize、
  scenario 別 variant を構造化 (user 提言の方向)
- **分布形式 prediction** — 「30% A / 30% B / 40% C」 で LLM judgment variance
  を明示的にモデリング
- **scenario 多様化** — memory / eval / 3-agent chain / 非日本語 input を候補に

---

## 教訓

1. **「観測しない」 決断が観測を鮮明にする**: attractor は触らないと決めた
   batch で、 attractor の存在が最も明確に記録された。 fix しない観測の価値は
   「何ができないか」 の定量的確定にある
2. **prediction の外れ方に情報がある**: 1.5/5 という低精度は失敗でなく、
   「LLM judgment のばらつき範囲が prediction モデルより広い」 という知識の
   獲得だった。 分布形式 prediction への進化の動機はここにある
3. **root cause と観測対象は別 layer にある**: B2-M2 の再現を狙って tool_failed
   を踏ませようとしたが、 LLM は tool call 自体を選ばなかった。 観測設計が
   仮定した経路と LLM が実際に選んだ経路は別物だった。 fix の前に root cause
   investigation が必要
4. **「LLM に path を扱わせない」 は設計原則**: B6-S1-H1 の本質的修正方向。
   prompt rule で補完 instruction を追加する対症療法より、 OS が解決した値を
   渡す構造変更が根本解。 G2 preprocessor 化 (= LLM call 0) と同じ思想
5. **user 介在点が pivot を生む**: A4 review が「前の turn の提案を撤回する」
   という判断に繋がった。 user feedback は「方向感」 として受け、 実装に
   落とす前に根本の設計と照合する習慣が fix の品質を上げる
6. **fix の dependency を tracker に明示する**: B4-M1 fix は B6-S1-H1 fix が
   先行条件。 dependency 関係が暗黙になると fix 設計時に前提が崩れた状態で
   走ることになる。 tracker に「blocks / blocked-by」 の記述を追加する候補
7. **dogfood retest が inconclusive でも、 副次的に新 infra bug を炙り出す**: 仮説
   (a) の dogfood 観測が失敗した理由は仮説 (a) でなく、 chat trusted python gap
   + workspace boundary mismatch の 2 件だった。 「観測できなかった = 何も学ばなかった」
   ではなく、 「インフラ層の gap を発見した」 という観点で dogfood retest の
   価値を評価し直す必要がある
8. **Tier 3 LLMReplay は dogfood retest の代替でなく補完**: Tier 3 は「そのような
   応答が来た時に正しく動作する」 を pin し、 regression guard として機能する。
   dogfood は「実 LLM が実際にそう振る舞うか」 を観測する。 両経路は目的が
   別であり、 どちらかが inconclusive でも他方の価値は毀損されない
9. **permission system と workspace boundary の二重 gate は gap を生む**: 同じ
   semantics (= アクセス許可) を 2 つの独立したレイヤーが実装すると、 片方を
   bypass した時に他方が拒否する gap が生まれる。 設計 review で「同じ semantics
   の gate は 1 箇所に集約」 という invariant を tracker に pin する候補

---

## 関連 docs

- [findings.md](findings.md) — 全体 narrative + A4 review 記録 + post-S5 wave 追記
- [findings/B6-S{1-5}-observation.md](findings/) — 5 scenario raw 観測
- [findings/B6-S1-M1-hypothesis-a-verify.md](findings/B6-S1-M1-hypothesis-a-verify.md) — 仮説 (a) 初回 verify (inconclusive)
- [findings/B6-S1-M1-hypothesis-a-tier3-verify.md](findings/B6-S1-M1-hypothesis-a-tier3-verify.md) — Tier 3 LLMReplay verified (regression guard)
- [findings/B6-S1-M1-hypothesis-a-retest.md](findings/B6-S1-M1-hypothesis-a-retest.md) — dogfood retest (inconclusive + 新 infra bug 2 件)
- [giveup-tracker.md](../giveup-tracker.md) — G3/G10/G12 update + G13/G14 resolved
- [batch 5 retro](../2026-05-04-batch-5-fix-verify/retrospective.md) — 直前 batch (= G2 / G12 化判断)
- [prelude.md](prelude.md) — batch 6 の前夜
