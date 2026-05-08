# F11 [MED]: router、 日本語が苦手

> 一行で: router の fallback / clarifying path だけ英語固定。 ja 設定しても
> 抜けてくる。

| Field | Value |
|---|---|
| Severity | MED |
| Status | **fixed** at `e59cead` (F8 と同 commit) |
| Scenario | scenario 2 + scenario 3 |
| Found | 2026-05-04 |

---

## 観測

scenario 2 (英語 retry budget エラー) と scenario 3 (英語 clarifying question)
で、 router の fallback path / clarifying path が日本語 user に英語応答を
返した。 user が日本語で chat してるのに、 internal error は英語で出る。

## 原因

- output_language config を local に未設定だった (= dogfood 進行中に発覚、
  `reyn.local.yaml` に `output_language: ja` 追加で対処)
- だが追加後の scenario は再実行していないので、 fallback path が
  output_language を尊重するか未確認
- 仮に config 反映後も英語ならば、 fallback path が hardcoded English

## 影響

- 日本語 user が「英語のエラー → 何書いてあるか分からない / non-actionable」
  という二重苦
- 国際化レベルが「ja 設定すれば応答は日本語」 までは行ってるが、 internal
  error path は穴あき

## 修正 (commit `e59cead`)

router system prompt の Behaviour section の generic な "Match the user's
language" を **explicit な BCP-47 directive** に置換:

```
Always reply in language: {output_language}.
Do NOT switch language even for error messages or clarifying questions.
```

- `build_system_prompt` に `output_language: str = "en"` 引数追加
- `RouterLoopHost` protocol に `output_language: str` 追加
- `ChatSession` から `output_language` を thread

`Always reply in language: ja` のように具体的言語コードが prompt に含まれる
ことで、 弱モデルでも「clarifying question / error path で英語にスイッチ
する」 attractor が抑えられる。 4 件の Tier 2 test で pin。

## 後続 (Wave B 以降)

- batch 2 で再現確認 — fallback path / clarifying path が ja で出るか
- hardcoded English がまだ残ってないか grep audit
- 多言語 (= en / ja 以外) 対応の設計判断 — full BCP-47 サポートか、
  enterprise-targeted な ja/en 限定か

## Q2 follow-up (= F11 修正の上位互換、 user 指摘 2026-05-04)

F11 修正後 user 指摘:

> output_language はユーザの設定がなければ llm へのプロンプトから外す方が
> 良いのかもね

> 言語のフォールバックは禁止じゃないの？ja へのフォールバックは最悪だよ。
> 世界中の人に使ってもらいたいんだから。

つまり F11 の修正で `Always reply in language: ja` を出すようにしたが、
**user が config で output_language を設定していないとき** にも ja default が
prompt に baked される → 英語 user に Japanese forced で UX 破綻 risk。
global 化を目指す project では「設定しなければ言語強制しない」 が正解。

修正内容 (= Q2 wave):

- `ReynConfig.output_language: str | None = None` に変更 (= 「未設定」 を None で signal)
- 全 LLM-facing path (chat router / phase / skill / sub_skill / preprocessor /
  postprocessor / control_ir_executor / RunSkillIROp / ContextFrame) を
  Optional[str] 透過対応
- `build_system_prompt`: None なら `Always reply in language: ...` directive を
  完全 omit (= LLM が user input 言語で自然推論)
- `_ROUTER_RETRY_EXHAUSTED_MSG`: None なら en fallback (= internal error
  string の安全 default、 ja/en 以外への regional default は禁止)
- `or "ja"` 形式の silent fallback を user-facing flow から全廃 (CLI run /
  eval / chat session の skill spawn 経路 4 箇所)

5 件の Tier 2 test 追加 (Q2):
- config: 未設定 → None
- config: 明示値 → そのまま
- config: 空文字列 → None (= override で「言語固定外したい」 を表現可能)
- system prompt: None → directive 無し
- retry exhausted: None → en fallback (regional default に滑り込まない)
