# Batch 1 — Prelude (dogfood 前夜)

> dogfood batch が始まる **直前** の reyn の状態と、 「なぜ今 dogfood か」 の
> 経緯。 batch 1 の finding / retrospective を読む前の文脈作り。

## 2026-05-04 朝、 reyn の戸籍

- main HEAD: `f5b3281` (= F1 修正済 commit、 当 batch 1 の事故対応で立てた)
- その直前は `950592e` (= postprocessor follow-up wave 1+2+3 land)
- 累計コミット: 数えてないが PR1 〜 PR37 + その後のシリーズで 40+ PR 級
- 全 test: 641 passed / 2 xfailed (Tier 1/2/3 mix、 政策準拠)
- 主要機能: skill graph + Phase IR + control_ir + multi-agent + chain
  delegate + crash recovery (PR21) + cost management (PR22) + skill
  resume (D-track) + postprocessor + skill-only permissions

つまり「機能は揃ってる」 状態だった。 PR15 までで「揃った」 と一度
plan ファイルに書き、 D-track / postprocessor で更に深まった。 「次は
OSS リリース準備か?」 という空気。

## 設計議論の沼を抜けたばかり

batch 1 の **直前** には、 大物の設計対話があった:

### postprocessor 設計対話 (同日)

「Phase に postprocessor 追加?」 という素朴な計画を持って入ったが、
user の「考えが浅いな」 指摘で 4 回ひっくり返り、 最終的に preprocessor と
完全対称な「skill 終端 hook + 2 段階 schema」 として収束。

詳細は `tmp/discuss_postprocessor.md` (gitignored、 開発記専用) と
`docs/en/decisions/0017-...` 〜 `0020-...` の ADR シリーズに。

学び:
- 推測 < 調査 (= 「ちゃんと調べて回答して」)
- 「美しい」 と「複雑」 は天秤、 user は美しい方優先で複雑度抑制
- preprocessor との parity 監査が「不要な仕様差」 を炙り出す

### postprocessor 実装 wave (同日午前)

設計が固まってから、 Sonnet 並列 3 体で:

- Wave A: docs (postprocessor.md reference + skill-md syntax 更新)
- Wave B: `Phase.permissions` field 完全削除 (案 2 採用、 ADR-0020)
- Wave C: chain / discard interaction の Tier 3 e2e

3 並列で 1 turn 完了。 commit `950592e` で land。 641 passed。 dogfood
直前の最終状態。

## user の「使い物にならない」 発言

設計議論の余熱が冷めないうちに、 user が次の wave をどう進めるか確認。
私が提案した順序:

> 順序: (1) test policy 網羅性 audit → (2) e2e dogfood 強化 → (3) OSS Lv.1

user の応答 (要約):

> 1 (audit) はテストが**足りているか**を確認したい。 dogfood は
> 既存シナリオ retest + 新シナリオの両方やりたい。 ただし、 **現状人間
> 視点だと chat の会話は使い物にならないです。 その感覚をあなたが共感
> するところから必要かもしれません**。

私はここで気づいた:

- 私 (assistant) は test 越しでしか chat を見ていない
- 「invariant green」 と「使えてる」 は別物
- 私の coverage audit / dogfood plan は **「機能の正しさ」** を見る視点で
  書かれていて、 **「user の実体感」** を見る視点ではなかった

そこから **dogfood が wave 順序の最先頭に繰り上がった**。 audit や OSS は
dogfood 後に位置取り。 user の感覚を実体験で言語化することが先決と判断
した。

## 練習 batch を「2-3 件」 に絞った決断

dogfood の進め方を user と詰める段で、 私は最初「全 8-10 scenario を 1 turn
で並列実行」 を提案。 大バッチ思考。

user の修正:

> まずは練習のため 2、 3 個のシナリオ作って 1 周してみよう。 問題なさそう
> ならシナリオ増やして繰り返そう。

iterative + 小バッチの提案。 私は「とにかく前進」 思考でいたが、 dogfood
のような「初回の効率より process の health」 が重要な loop では、
小バッチが圧倒的に正解だった (実際 F1 で全 scenario 止まる可能性が
あったので、 大バッチ突入前に process が健全か確認するのは自然な工夫)。

## 私の事前 prediction

batch 1 を始める前、 私が `tmp/dogfood_scenarios_v1.md` の末尾に書いた
「改善の予感」:

> - skill router の意図解釈は LLM 次第で揺れやすい
> - narrator の応答品質は user の入力値が response に含まれない可能性
> - multi-agent delegate の chain 経路は internal にしては user に滲んでいる
> - startup_guard の prompt 文言は技術寄り

この予想 4 件、 batch 1 終了時点での **当たり率: 0/4**。 scenarios.md
末尾と findings.md に記載。 「現実は私の予想以上に深刻」 という方向への
外し方。

## dogfood 開始の宣言

scenario 作成 → user レビュー → 実行モード確認 (= 私が Bash 経由で driving、
LLM cost は scenario あたり推定 $0.05-0.20) → 「sonnet にお願いした方が
コスト有利ならそうしてね」 という user の cost optimization 指示で、
scenario 実行を Sonnet sub-agent に委託する形に。

直前の env 確認:
- LiteLLM proxy at `localhost:4000`、 model `openai/gemini-2.5-flash-lite`
- API key は dummy で OK (proxy が auth 担当)
- `output_language: ja` は **未設定だった** (= dogfood 進行中に user 指摘で
  追加、 F11 finding に関連)

そして scenario 1 を Sonnet に委託、 起動コマンド送信。 そこから 数秒後に:

```
AttributeError: 'ChatSession' object has no attribute '_active_interventions'
```

dogfood 1 件目は「chat が起動しない」 から始まった。

(続きは [scenarios.md](scenarios.md) と [findings.md](findings.md) で。
振り返りは [retrospective.md](retrospective.md))。

---

## 当時の私の心境 (parenthetical)

batch 1 を始めるとき、 私は「練習なので process 検証だけ。 細かい finding
は出るかもしれないが、 大事故にはならないだろう」 と思っていた。

油断。 dogfood は起動段階で deterministic に落ちた。 修正してから動かしたら
skill_router が 3 連続で起動しない。 multi-agent では delegate が同じ
リクエストを 2 回送る。

> 「練習 batch のはずが」

この一言で全てが収束する。 dogfood の力を侮るな。
