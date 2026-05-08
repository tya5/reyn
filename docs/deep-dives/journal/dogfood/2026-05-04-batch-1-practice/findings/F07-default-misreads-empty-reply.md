# F7 [MED]: default、 空 reply を聞いて「ダメだったか」 と判断する

> 一行で: default、 specialist の空 reply を「失敗」 と判定して再 delegate。
> retry budget 即枯渇。

| Field | Value |
|---|---|
| Severity | MED |
| Status | **fixed** at `9e8126c` (F6 修正の implicit 効果) |
| Scenario | scenario 2 (specialist agent → curry recipe) |
| Found | 2026-05-04 |

> Context: F5-F8 連鎖事故 cascade の 3 番目。 F6 が直接原因、 F7 はその
> 反応として現れる「ダメ押し」。

---

## 観測

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

## 修正 (commit `9e8126c`)

F6 の構造化マーカー導入で **implicit に解消**。 specialist が "" ではなく
明示的失敗マーカーを送るようになったため:

- 受信側 LLM は「peer は失敗した」 と明確に解釈
- 同一ターン内の retry を起こさない (= 構造化メッセージで「これ以上の
  retry は無意味」 と LLM が判断)
- F7 cascade は再発しない

別途 router 側で「空文字列 → in-progress 解釈」 ルールを足す案は **採用せず**。
理由: F6 fix で根本原因が消えるので、 防御層を 2 重に積む必要なし。
batch 2 で再現確認 (regression net)。

## 教訓 / 後続

- 連鎖 bug は **根本原因** (= F6) を直すと **下流** (= F7) も同時に消える
  pattern が理想。 多層防御は 1 層目が確実に効く確証があるとき以外は
  避ける
- batch 2 の multi-agent シナリオで F5+F6+F7 cascade が再発しないか確認
