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
