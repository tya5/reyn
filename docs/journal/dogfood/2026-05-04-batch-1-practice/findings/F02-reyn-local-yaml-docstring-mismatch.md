# F2 [LOW]: reyn.local.yaml は loaded されているが、 docs はそれを知らない

> 一行で: `reyn.local.yaml` は実際 load されているのに、 `config.py` の
> docstring には「そんな file 知らん」 と書いてある。

| Field | Value |
|---|---|
| Severity | LOW |
| Status | deferred (Wave B coverage audit で実施) |
| Scenario | scenario 1 (config 設定中に発見) |
| Found | 2026-05-04 |

---

## 観測

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

## 影響

- 機能として動作する: `reyn.local.yaml` に書いた `api_base:
  http://localhost:4000` は config に正しく反映される (= dogfood で確認済)
- 新規 user は「結局どの file に書くの?」 で迷う
- 内部読者 (= refactor / debug する開発者) も自分が書いた docs と code が
  一致していない罠を踏みうる

## 後回し理由

dogfood の主観点 (= chat 会話 UX) と独立。 Wave B coverage audit で docs
/ config 系 finding をまとめて整理。

## 提案修正 (Wave B で実施)

- `config.py` docstring に `reyn.local.yaml` 行を追加
- `cli/templates.py` のコメントも整える
- 一段方針: 「project レベル override 2 段持つ」 を docs で明示するか、
  さもなくば `reyn.local.yaml` を deprecated にして 1 段にするか
  (個人的には後者の方が単純)
