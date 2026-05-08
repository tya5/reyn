# Batch 1 (Practice) — Retrospective

> dogfood 進行中の user との対話の振り返り。 process 自体の学びをここに
> 残す (= 次 batch 以降のテンプレに使えるように)。

## ハイライト 5 連発

### 1. 「現状人間視点だと chat の会話は使い物にならないです」

dogfood の起点になった user の一言。 この時点で私 (assistant) は test 越し
で「invariant green」 を確認していて、 chat が「使えてる」 と暗黙前提
していた。 user の一言で、 開発者の test 観点と user の実体感の溝が
明確になった。

dogfood という format が選ばれた理由はここに集約されている: shadow しても
見えないものを、 実 LLM 経由で 1 ターンずつ触って見る。 自動 test が
greenでも、 routing 失敗 / 連鎖 bug は見える。

### 2. 「考えが浅いな」 (postprocessor 議論より)

これは batch 1 dogfood 自体ではなく、 直前の postprocessor 設計議論での
user 発言。 dogfood scenario の作成中にも同じ姿勢が efficient だった:

- 私が出した素朴 4 候補 → user が即「P1 違反」 を指摘
- 私が「逆推論案?」 と複雑化 → user が「いや LLM はそのまま out_schema 作る、
  postprocessor が新 schema に作り変えるだけ」 とシンプルに framing
- 私が「op 制約で run_skill 禁止」 → user が「preprocessor は run_skill
  禁止してないよ。 ちゃんと調べて」 → 調査して撤回

dogfood retro として活きたのは、 この **「推測で書くな、 調べてから書け」**
というスタンス。 私は scenario 作成時に「simple_memo_app」 を当然のように
書いたが、 これも `reyn skills` で確認すれば最初から `text_summarizer`
にできた。 推測コストの累積が dogfood の進行を遅らせる。

### 3. 「練習として 2-3 個で 1 周してみよう」

user の判断で batch サイズを 2-3 に絞った決断は、 結果として正解だった
(findings.md「バッチサイズの教訓」 参照)。 理由:

- F1 (起動しない) で全 scenario が止まった可能性
- 小バッチで process 検証 → 大バッチに展開という pacing が dogfood に
  natural

私は最初「全 8-10 scenario を 1 turn で」 と提案していたが、 これは
「とにかく前進」 思考。 user の小バッチ提案が dogfood の health を守った。

### 4. 「sonnet にお願いした方がコスト有利ならそうしてね」

cost optimization の指示。 dogfood 実行は LLM cost (= reyn の chat call)
+ 私の thinking cost の 2 軸。 reyn cost は scenario が同じなら一定だが、
私の thinking cost は Opus vs Sonnet で変わる。

scenario 実行 (= bash 起動 + 出力観測 + finding 記述) は Sonnet で十分な
ので、 user の指示で全 3 scenario を Sonnet 委託に切替えた。 私は
aggregate / classify / docs 整形に集中。 適切な分業。

### 5. 「コンテンツなのでユーモアも忘れずに」

dogfood を tmp/ ではなく docs/ に残すと user が言ったタイミングで、
「コンテンツとして読み物にしろ」 という方向性が確定。 「真面目な test
report」 ではなく「事件記録」 として書く。

これは reyn の dev chronicle 全体に効く方針。 ADR / discussion log /
dogfood findings — どれも「正しい」 だけでなく「読まれる」 ものにする。
この journal/ ディレクトリは その実験場。

---

## process 上の学び

### scenario 作成

- **既存 skill 一覧を `reyn skills` で先に確認** する。 推測で skill 名を
  書くと、 私がやったように「simple_memo_app は available 一覧に存在しない」
  問題が起きる
- **小バッチ (= 2-3 scenario) で先に 1 周** 回す。 大バッチ前に process が
  健全か確認
- 各 scenario の **観測ポイント (6 軸)** を事前に決めておく。 finding の
  漏れを防ぐ + finding の比較がしやすい

### 実行

- LLM cost を伴う実行は **Sonnet sub-agent** に委託、 Opus は orchestration
  + aggregation
- `reyn chat --cui --no-restore` を **Bash 経由 stdin pipe** で driving 可能。
  TUI ではなく CUI mode が必須
- timeout は **十分に長く** (sleep 130s 程度) — LLM 応答 + chain 経由 reply
  + retry の合計が想定以上

### 報告

- **6 軸 (応答品質 / 意図解釈 / 待ち時間 / 見せ方 / エラー UX / state 整合性)
  を毎 scenario で必ず埋める**。 finding が漏れる主原因は観点不足
- finding には **HIGH / MED / LOW** を **assistant の主観で初期 tag**、
  user の感覚 review (A4) で reclassify
- 中身は技術的に正確 + 文章は読み物として面白い (= ユーモア)。 両立は
  可能、 文章レベルで遊ぶ

### user との loop

- A1 (scenario 作成) → A2 (user review) → A3 (実行) → A4 (user 感覚 review)
  → A5 (分類) の 5 step は dogfood の natural rhythm
- user の感覚 review (A4) は dogfood の最後の山。 私が LOW と思ったものが
  user 視点で HIGH かもしれない、 という ground truth との突合せ
- A4 review 結果はこの retrospective.md の「user 感覚との差」 section に
  追記 (= まだ未実施、 placeholder)

---

## user 感覚との差 (A4 結果)

**user 全件合意** (2026-05-04)。 私が assistant 主観で付けた重要度
(HIGH / MED / LOW) の 11 件すべてに、 user の感覚との差は **無し**。

```
> おk。全部合意です。
```

— user (A4 review、 2026-05-04)

意外と言えば意外、 当然と言えば当然。 0/3 という skill_router 起動成功率
の数値が圧倒的だったので、 重要度判定で揉める要素が少なかった。 もし routing
率がもう少し高くて (例: 2/3) 、 かつ multi-agent が部分的に動いていた
ら、 細かい finding (= F4 / F11 等) で「これ HIGH では?」 「いや MED で
十分」 という議論があったかもしれない。

開発者と user の感覚が一致したのは、 おそらく:

- batch 1 の bug が **明白に重い** (= 起動できない / router 動かない /
  delegate 重複) で議論の余地が少ない
- 私が finding を書く時点で、 6 軸観察 + 1 行サマリ + 詳細を分けて構造化
  したので、 user が「あれ?」 「いや」 と感じたら即気づける形になっていた

次 batch 以降、 もう少し subtle な finding が出る場面 (= UX 微妙、 待ち
時間長すぎる、 文章雑等) で初めて感覚差が出るかも。 batch 2 で要観察。

---

## 次 batch への申し送り

batch 2 (本格 batch) を始めるとき、 ここから引き継ぐもの:

- **scenario template**: scenario 作成時の template = 目的 / Setup / Action
  / 期待結果 / 観測ポイント (6 軸) を埋める形式
- **observation 6 軸**: 応答品質 / 意図解釈 / 待ち時間 / 見せ方 / エラー UX
  / state 整合性
- **finding format**: ID + 重要度 + タイトル + 観測 + 原因 + 影響 + 後続
- **執筆スタイル**: 「事件記録」 風、 ユーモア込み、 技術的正確さ維持
- **batch 1 の HIGH bugs**: F3 / F9 (router 不機嫌)、 F5-F8 (multi-agent
  連鎖)、 F10 (MCP server 未設定) を batch 2 で再現確認 (regression net)

batch 2 の対象 scenario 候補 (= batch 1 で抜けたもの):

- skill 起動 + ask_user → user 回答 → skill 継続
- skill 中断 (Ctrl-C) → 再起動 → auto-resume
- nested skill (parent → run_skill child)
- postprocessor あり skill の e2e
- chat compaction 境界 (= 30+ ターン後の挙動)
- memory 操作 (remember / recall)
- skill_improver / eval_builder の実用 flow

batch 1 の HIGH bug が直ってから batch 2 を回す方が、 finding が batch 1
の bug ノイズに埋もれない。 bug fix PR の wave を挟んでから batch 2 が
妥当。
