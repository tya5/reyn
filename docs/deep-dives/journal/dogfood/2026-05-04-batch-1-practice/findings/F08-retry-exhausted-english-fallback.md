# F8 [MED]: 諦めるときくらい日本語で謝ってほしい

> 一行で: 諦めるときに出るエラー文が英語。 user は日本語で話してた。
> 内容も「rephrase してね」 で誤誘導。

| Field | Value |
|---|---|
| Severity | MED |
| Status | **fixed** at `e59cead` (F11 と同 commit) |
| Scenario | scenario 2 (cap exhaustion) |
| Found | 2026-05-04 |

> Context: F5-F8 連鎖事故 cascade の最後の仕上げ。 F5+F6+F7 で retry budget
> が枯渇した結果、 user に届く最終メッセージが英語 + non-actionable。

---

## 観測

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

## 原因

`session.py:_emit_router_cap_exhausted_user` 内に hardcoded English string。
output_language config を見ていなかった。

## 修正 (commit `e59cead`)

`_ROUTER_RETRY_EXHAUSTED_MSG` dict (ja / en) を導入し、
`output_language` を見て選択。 unsupported language code は en に
fallback。

```python
_ROUTER_RETRY_EXHAUSTED_MSG: dict[str, str] = {
    "ja": "このターン内で処理を完結できませんでした (router 予算使い切り)。"
          " 別の言い回しで試すか、リクエストを分割してみてください。",
    "en": "I couldn't find a way to handle that within this turn's routing budget."
          " Please try rephrasing or breaking the request into smaller pieces.",
}
```

3 件の Tier 2 test (ja / en / unsupported fallback) で pin。

## 教訓 / 後続

- output_language を尊重する path / しない path の audit が必要
  (= F11 と同様)。 hardcoded English が他にも残っていないか grep audit が
  Wave B で必要
- 実は内容も微妙: "rephrase or break the request" は今回の bug を解決
  しないので user を誤誘導している。 root cause が「user の表現問題」
  ではないとき別の表現を出す option も検討
