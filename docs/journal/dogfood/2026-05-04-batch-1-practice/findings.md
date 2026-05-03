# Batch 1 (Practice) — Findings

> 練習 batch のつもりが、 起動段階で躓き、 そこから先も**全 scenario で
> skill_router が起動しない** という結果になった事件記録。

## 概要

> 「で、 何が起きたの?」 の一行サマリ。 詳細は各 section へ。

| ID | 重要度 | 一行で言うと | 状態 |
|---|---|---|---|
| [F1](#f1-chat-に話しかけたかったのに-chat-が起動を拒否した話) | HIGH | reyn chat を起動した瞬間 `AttributeError`。 「Did you mean」 まで親切な Python だが起動はしない | **fixed** at `f5b3281` |
| [F2](#f2-reynlocalyaml-は-loaded-されているが-docs-はそれを知らない) | LOW | `reyn.local.yaml` は実際 load されているのに、 `config.py` の docstring には「そんな file 知らん」 と書いてある | deferred (Wave B) |
| [F3](#f3-skill_router-仕事しない大将) | HIGH | 「要約して」 とお願いしたのに `text_summarizer` skill は呼ばれず、 LLM が「自分でやれます」 と直答 | open |
| [F4](#f4-cost-永遠の-0) | LOW | LLM 応答は来てるのに `cost -- prompt=0 completion=0 total=0`。 永遠の 0 円 | open |
| [F5](#f5-high-delegate-言ってないのに-2-回送る) | HIGH | LLM が `delegate_to_agent` を 1 回呼んだのに、 specialist の inbox には同じ依頼が **2 件** 届く | open |
| [F6](#f6-high-specialist-まだ答えてないのに答えましたを送る) | HIGH | specialist 側、 LLM がまだ考え中なのに「答えました (中身: 空)」 を default に送りつける | open |
| [F7](#f7-med-default-空-reply-を聞いてダメだったかと判断する) | MED | default、 specialist の空 reply を「失敗」 と判定して再 delegate。 retry budget 即枯渇 | open |
| [F8](#f8-med-諦めるときくらい日本語で謝ってほしい) | MED | 諦めるときに出るエラー文が英語。 user は日本語で話してた。 内容も「rephrase してね」 で誤誘導 | open |
| [F9](#f9-skill-名を明示してもなお-router-は応えない) | HIGH | `read_local_files skill で〜` と skill 名を本文に直書きしても router は無視。 routing 0/3 | open |
| [F10](#f10-filesystem-mcp-は箱の中で寝ている) | HIGH | `read_local_files` は filesystem MCP を要求するが、 そんな MCP server はどこにも設定されていない | open |
| [F11](#f11-router-日本語が苦手) | MED | router の fallback / clarifying path だけ英語固定。 ja 設定しても抜けてくる | open |

**skill_router の起動成功率: 0/3。** これが batch 1 の headline。

---

## F1: chat に話しかけたかったのに、 chat が起動を拒否した話

### 観測

scenario 1 を試そうと `reyn chat default --cui --no-restore` を起動した瞬間、
chat session attach の最終 step で `AttributeError`:

```python
File "src/reyn/chat/registry.py", line 599, in attach
    for iv in list(new_session._active_interventions.values()):
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AttributeError: 'ChatSession' object has no attribute '_active_interventions'.
Did you mean: '_announce_intervention'?
```

「Did you mean」 まで丁寧に教えてくれる Python だが、 教えてくれた候補は
全然違う method 名なので役に立たない。

### 原因

`PR-refactor-session-1` (commit `41ec4cb`、 wave 1C) で `ChatSession` から
`InterventionRegistry` service を抽出し、 active intervention queue を
`session._interventions` に移動した。 ところが `chat/registry.py:attach()`
の最終 block で旧 attribute `_active_interventions` を参照したまま、
誰にも気づかれず main に乗っかっていた。

PR-refactor-session-1 wave 2 で「session-level Tier 2 invariants」 を 10 件
追加したが、 **attach 後の pending intervention 再 announce 経路は test
範囲外** だった。 TDD で「赤 → 緑 → refactor」 を回したつもりが、 そもそも
「赤」 になる test が無かった経路で regression が滑り込んだ事例。

### 修正

`chat/registry.py:599` を `InterventionRegistry.list_active()` 経由に書き換え:

```python
# Before (broken)
for iv in list(new_session._active_interventions.values()):
    ...

# After
for iv in new_session._interventions.list_active():
    ...
```

修正後 641 passed (regression net 健全)。 commit `f5b3281` で land。

### 教訓

- TDD の「赤 → 緑」 で test を書く前に、 **どの経路を pin するか** を意識的に
  enumerate しないと、 test policy 守ってても regression は普通に起きる
- service 抽出 refactor で「呼び出し側の参照を全部移行できたか」 を機械的
  に確認する仕組み (= 静的解析 / grep audit) が欲しい。 案: 抽出時に
  「旧 attribute を deprecated property に格下げして warning 出す」
- coverage gap として Wave B で attach 経路 + pending intervention
  combination の Tier 2 を追加

---

## F2: reyn.local.yaml は loaded されているが、 docs はそれを知らない

### 観測

scenario 1 を実行する前に「LiteLLM proxy の api_base はどこに書く?」 を
user と確認した際、 user が「reyn.local.yaml て機能として搭載してたっけ?
.reyn/config.yaml だったような」 と疑問を呈した。

調査結果:

```
src/reyn/config.py:427-442 で実際に load される 4 file (順序):
  ~/.reyn/config.yaml         user global
  <project>/reyn.yaml         project (committed)
  <project>/reyn.local.yaml   ← load されている
  <project>/.reyn/config.yaml override of overrides
```

しかし同 file の docstring (L4-9):

```
Priority (lowest → highest):
  built-in defaults
  ~/.reyn/config.yaml         user global
  <project>/reyn.yaml         project (git managed)
  <project>/.reyn/config.yaml local overrides (gitignored)
```

`reyn.local.yaml` の行が **docstring からだけ** 抜けている。 一方で
`reyn.yaml` のコメントには `reyn.local.yaml` が mention されており
(L3)、 reality と docs の間で内部矛盾が起きている。

### 影響

- 機能として動作する: `reyn.local.yaml` に書いた `api_base:
  http://localhost:4000` は config に正しく反映される (= dogfood で確認済)
- 新規 user は「結局どの file に書くの?」 で迷う
- 内部読者 (= refactor / debug する開発者) も自分が書いた docs と code が
  一致していない罠を踏みうる

### 後回し理由

dogfood の主観点 (= chat 会話 UX) と独立。 Wave B coverage audit で docs
/ config 系 finding をまとめて整理。

### 提案修正 (Wave B で実施)

- `config.py` docstring に `reyn.local.yaml` 行を追加
- `cli/templates.py` のコメントも整える
- 一段方針: 「project レベル override 2 段持つ」 を docs で明示するか、
  さもなくば `reyn.local.yaml` を deprecated にして 1 段にするか
  (個人的には後者の方が単純)

---

## F3: skill_router、 仕事しない大将

### 観測

scenario 1 で「次の英文を 3 つの bullet point に要約して」 と送信。 期待は
skill_router が `text_summarizer` skill を起動。 実態:

```
events/agents/default/skill_runs/2026-05-04*  → 存在しない
WAL: skill_dispatch event           → 存在しない
WAL tail:
  seq=205 inbox_put     (= user message)
  seq=206 inbox_consume (= agent picks it up)
  (続き — skill 関連 event 一切なし)
agent 応答:
  "* Python は 1991 年に Guido van Rossum によって作成された…"
  (= LLM が direct reply、 約 2 秒)
```

要約の中身は正しい。 日本語も自然。 だが **skill は呼ばれていない**。
LLM が直接答えただけ。

### つまり何が起きたか

Reyn の chat router は PR35 で native tool_use loop に置き換わった。 LLM が
利用可能 tool 一覧 (= skill / agent / memory / file / mcp) を見て、
「skill を起動する」 のか「直接答える」 のかを判断する。 今回 LLM は
「自分で要約できる」 と判断 (して問題ないと言えば問題ない) し、
text_summarizer を選ばなかった。

これは 2 通りの解釈:

- **(a) bug**: skill が catalog に登録されていて、 user が暗黙に「要約タスク」
  を頼んだのに、 router が skill を選ばないのは routing 失敗
- **(b) feature**: 軽量タスクは直接答えていい、 skill 起動は明示的に user
  が指定したときだけ

PR35 の dogfood 文脈 (memory: `project_dogfood_post_pr35.md`) では
「intent classification 改善ライン (R2-R7) は dogfood で urgency 出ず」
と記載されており、 当時は (b) として acceptable とされていた。 だが **F9
(scenario 3) で skill 名を明示してすら router が起動しなかった** ことを
合わせると、 これは (a) bug の方向に振れる。

### 影響

- text_summarizer skill が catalog に居る意味が薄い (= 呼ばれないので)
- skill 経由の固定品質パイプライン (= phase 構造、 schema 検証、
  preprocessor / postprocessor) を user が体感できない

### 後続 candidate

- skill_router の routing 判定の調査 (= LLM への tool offering / system
  prompt / threshold が適切か)
- gemini-2.5-flash-lite の routing 精度問題なら、 router を strong model
  に固定する option

---

## F4: cost、 永遠の 0

### 観測

scenario 1 完了後、 chat の最後に毎回:

```
cost --  prompt=0 completion=0 total=0
```

LiteLLM proxy 経由なので token カウントが取れていない可能性。 LLM 応答は
正常に来ている (= 課金は発生しているはず) が、 reyn 側の表示は永遠の 0。

### 影響

- BudgetTracker (PR22 + R-D8) の永続化が landed したが、 入力 0/0/0 で
  集計しても意味がない
- user が「どれくらい使ったか」 を chat で確認できない

### Cause hypothesis

- LiteLLM proxy の response に `usage` field が含まれていない
- もしくは reyn の cost parser が proxy response 形式に対応していない
- もしくは litellm SDK が proxy response から usage を抽出できていない

### 優先度

LOW。 機能の正しさには影響しないが、 BudgetTracker ↔ LiteLLM proxy
組み合わせの dogfood で初めて顕在化した integration 問題。 別 issue で
追跡。

---

## F5-F8: multi-agent delegate、 完全失敗の四重奏

scenario 2 (`specialist エージェントに「カレーの簡単な作り方」 を聞いて教えて`)
で発生した複合 bug。 単独でも HIGH だが、 4 件が連鎖して final UX が
破綻するという、 dogfood 史に残る (?) 連鎖事故。

### 全体の流れ (16 秒の悲劇)

```
08:26:23  user message receive
08:26:24  default agent の LLM、 delegate_to_agent を 1 回呼ぶ
          → だが WAL には inbox_put が **2 件** 書き込まれる   ← F5
08:26:25  specialist agent が 1 回目の request を consume、 LLM 起動
08:26:30  specialist が describe_skill tool を call
08:26:34  specialist の router_loop、 まだ skill 実行前なのに
          response: "" の agent_response を default に送る   ← F6
08:26:34  default 受領、 「peer 失敗」 と判断、 delegate を再試行   ← F7
08:26:39  specialist の router_loop、 また response: "" 送る
08:26:39  default、 また再試行 (retry 2/3)
08:26:40  default 側 retry budget 枯渇、 英語 fallback を表示   ← F8
08:26:42  specialist の LLM、 ようやくレシピ確定 (実は良質)
          → だが default 側 budget 切れで discarded
```

specialist は実は **完全に正しいカレーレシピを生成していた**。 ただ届く
頃には default 側で「もう諦めた」 状態だった。 user に届いたのは:

```
[error] Router exhausted retry budget (3/3) for this turn. Last reason: (none).
        Falling back to direct reply.
agent> I couldn't find a way to handle that within this turn's routing budget.
       Please try rephrasing or breaking the request into smaller pieces.
```

英語、 non-actionable、 何も解決しない。 specialist の労作はどこにも届かない。
これは「使い物にならない」 と user が言いたくなる体験そのもの。

### F5 [HIGH]: delegate、 言ってないのに 2 回送る

WAL trace:
```
08:26:24.745  inbox_put  target=specialist  msg_kind=agent_request
08:26:24.747  inbox_put  target=specialist  msg_kind=agent_request   ← 2 ms 差
```

events log でも `tool_called delegate_to_agent` が 2 件連続 (1 ターン内)。
LLM が同一 tool を 2 回 call しているか、 OS 側が 2 重 dispatch している。

`router_loop` で `delegate_to_agent` ハンドラの async 経路 (commit
`caaed75` で導入) を再点検する必要がある。 sync 経路と async 経路で event
emit が二重発火している hypothesis。

### F6 [HIGH]: specialist、 まだ答えてないのに「答えました」 を送る

specialist 側 events log:
```
08:26:30  tool_called  describe_skill                ← まだスキル決まってない
08:26:34  agent_message_sent  kind=agent_response  response: ""
08:26:39  agent_message_sent  kind=agent_response  response: ""
08:26:42  agent_message_sent  kind=agent_response  response: "<実際のレシピ>"
```

最初の 2 つの空 reply は明らかに早期送出。 `router_loop` が tool 呼び出し
結果を受け取った時点で「とりあえず agent_response 送る」 挙動になっている
ように見える。 LLM の final answer 確定まで agent_response 送出を遅らせる
必要がある。

これは F5 と独立だが連鎖して悪化させる (= 2 重 request × 早期空 reply ×
2 と、 ノイズが指数的)。

### F7 [MED]: default、 空 reply を聞いて「ダメだったか」 と判断する

default の router は specialist から `response: ""` を受け取り、 「peer
失敗」 と解釈して `delegate_to_agent` を再試行する。 だが空 reply は
「失敗」 ではなく「まだ in-progress」 という意味だった (specialist 側の
F6 bug によって早期送出されただけ)。

`response is None / "" / falsy` を区別する必要がある:
- `None`: 未到着 (timeout)
- `""`: 空文字 (= 失敗 or in-progress?)
- 中身あり: 成功

「空文字 = まだ来てない」 という解釈ルールが router に必要。 もしくは
specialist 側の F6 修正で空 reply 自体を排除。 後者のほうが筋が良さそう。

### F8 [MED]: 諦めるときくらい日本語で謝ってほしい

retry 枯渇時のメッセージ:

```
I couldn't find a way to handle that within this turn's routing budget.
Please try rephrasing or breaking the request into smaller pieces.
```

日本語で話しかけている user にこれが返る (= F11 と同根)。 user は
re-phrase しても解決しない (= bug なので)、 メッセージ内容も誤誘導。

加えて output_language の config を local で設定し忘れていたことも一因
(2026-05-04 の dogfood 進行中に user 指摘で `reyn.local.yaml` に
`output_language: ja` 追加)。 fallback path で output_language を尊重して
いない可能性も。

---

## F9: skill 名を明示してもなお、 router は応えない

### 観測

scenario 3 で「**read_local_files skill で** /path/to/README.md を読んで
要約して」 と、 **skill 名を本文に直書きして** リクエスト。 期待は router が
迷わず read_local_files を起動すること。

実態:

```
WAL tail:
  seq=221 inbox_put     (user message)
  seq=222 inbox_consume (agent)
  (続き — skill_dispatch なし)

events skill_runs/2026-05/  → 2026-05-04 entry 無し

agent 応答:
  "I noticed you asked to read and summarize the file ... twice.
   Could you please clarify if you need me to perform this action,
   or if you were testing something?"
```

skill router 起動の形跡無し。 LLM は「同じこと 2 回聞かれた」 と
hallucinate して clarifying question を返した (英語で。 F11)。

### つまり何が起きたか

F3 の延長線。 implicit な「要約して」 で起動しないだけでなく、 **explicit に
skill 名を挙げても起動しない**。 これは router の routing が壊れている
ことを示唆。

routing 失敗率: **0/3 across all scenarios**。 = router は仕事をしていない。

### 影響

- skill 経由のあらゆる UX (= startup_guard / phase / preprocessor /
  postprocessor / chain delegate) が user に届かない
- Reyn の中核価値 (= 構造化された LLM workflow) が user 視点で消失
- これが**「現状人間視点だと chat の会話は使い物にならない」 の正体の一部**

### 後続

- skill_router の routing 判定の徹底再点検
- system prompt / tool offering / 模範例 prompt の audit
- 場合によっては router を「強制 skill 起動 mode」 に切り替え可能にする
  flag (= operator が「skill 必ず通せ」 と指定できる)

---

## F10: filesystem MCP は箱の中で寝ている

### 観測

scenario 3 のもう一つの finding。 `read_local_files` skill は
`permissions: mcp: [filesystem]` を skill.md frontmatter で declare している。
しかし `reyn.yaml` / `reyn.local.yaml` どちらにも `mcp.servers.filesystem`
の登録がない。

つまり:

- skill 自体は MCP filesystem を使う前提で書かれている
- proj config には filesystem MCP server が登録されていない
- もし skill_router が起動していても、 MCP op 実行時に「server filesystem
  not configured」 エラーで落ちる

scenario 3 が観測したかった「permission prompt UX」 は、 仮に F9 が
解決しても **これ以前の段階で fail** する。

### 影響

- out-of-box experience: `reyn` を入れて `read_local_files` を試そうとしても
  動かない
- 「使い物にならない」 体験の典型例 — どこから手をつければいいか user
  に分からない

### 後続

- `reyn init` template に `mcp.servers.filesystem` の登録例を含める
- もしくは installer が optional に「filesystem MCP server を install する?」
  と聞く
- docs/en/reference/stdlib/read_local_files.md (= もしあれば) に setup 手順
  を冒頭に書く

---

## F11: router、 日本語が苦手

### 観測

scenario 2 (英語 retry budget エラー) と scenario 3 (英語 clarifying question)
で、 router の fallback path / clarifying path が日本語 user に英語応答を
返した。 user が日本語で chat してるのに、 internal error は英語で出る。

### 原因

- output_language config を local に未設定だった (= dogfood 進行中に発覚、
  `reyn.local.yaml` に `output_language: ja` 追加で対処)
- だが追加後の scenario は再実行していないので、 fallback path が
  output_language を尊重するか未確認
- 仮に config 反映後も英語ならば、 fallback path が hardcoded English

### 影響

- 日本語 user が「英語のエラー → 何書いてあるか分からない / non-actionable」
  という二重苦
- 国際化レベルが「ja 設定すれば応答は日本語」 までは行ってるが、 internal
  error path は穴あき

### 後続

- output_language ja 設定済 で再度 dogfood、 fallback path の挙動確認
- hardcoded English がまだ残ってないか grep audit
- error message を catalog 化、 ja / en の両方持つ

---

## まとめ — 練習 batch のはずが

3 scenario、 11 finding (重複除外で 10)。 練習バッチとしては多すぎる。

**全 scenario 共通の最重要 finding**:

1. **F1**: chat が起動しない (即修正済)
2. **F3 + F9**: skill_router が誰の言うことも聞かない (3/3 で起動失敗)
3. **F5 + F6 + F7 + F8**: multi-agent delegate が連鎖 bug で完全停止

これらは「現状人間視点だと chat の会話は使い物にならない」 の中身を構成
する根本問題と言ってよさそう。 dogfood で見えたものは大きい。

### バッチサイズの教訓

「練習として 2-3 scenario」 という user の指示は正しかった。 もし最初から
8-10 scenario 流していたら、 F1 の段階で全 scenario が die して何も
発見できなかった。 小バッチで先に process 検証する戦略が正解だった。

### 私の事前 prediction の精度

scenarios.md 末尾に書いた事前仮説 4 件:

- skill router の意図解釈は LLM 次第で揺れやすい — **外れ** (= router が
  そもそも起動しない)
- narrator の応答品質は phase 出力 + skill description だけで作る — **検証
  不能** (= skill 経由しないので narrator が呼ばれない)
- multi-agent delegate の chain 経路は internal にしては user に滲んでいる
  — **外れ** (= 滲む以前に動かない)
- startup_guard の prompt 文言は技術寄り — **検証不能** (= startup_guard
  までたどり着かない)

精度: 当たり 0/4。 ただし 「現実は私の予想以上に深刻」 という方向への
外し方なので、 dogfood の意義は十分。

### 次のアクション (= A5 分類後の followup PR)

`tmp/dogfood_findings_2026-05-04.md` の元 finding を整理してこの doc に
昇格。 元 raw findings は git 管理外なので tmp/ から削除して OK。

優先度ベースで next PR 候補:

1. **F3 + F9 集約 PR**: skill_router の routing 判定再点検 (HIGH)
2. **F5 PR**: delegate_to_agent 重複 inbox_put 修正 (HIGH)
3. **F6 PR**: specialist 早期空 agent_response 送出停止 (HIGH)
4. **F7 PR**: F6 fix 後の動作確認 (MED、 単独 PR にするか F6 と統合か判断)
5. **F8 + F11 PR**: error message 国際化 (MED)
6. **F10**: out-of-box experience 改善 (= 別 wave で OSS 準備に近い)
7. **F4**: BudgetTracker ↔ LiteLLM proxy integration (LOW)
8. **F2**: docs / config 整理 (Wave B で)

A4 で user に「私の感覚との差」 を share してもらい、 優先度を確定する
予定。 user は私が「LOW」 と書いたものを「うちのチームでは HIGH」 と
評価するかもしれないし、 逆もある。 そこが dogfood の最後の山。
