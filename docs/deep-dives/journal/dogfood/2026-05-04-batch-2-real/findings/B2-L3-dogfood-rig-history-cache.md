# B2-L3 [LOW]: dogfood rig の reset が不完全 — `history.jsonl` がキャッシュ replay

> 一行で: `rm -rf .reyn/state .reyn/events` では `history.jsonl` が残り、
> 前回 session の replay が起きる。 正しい reset は `rm -rf .reyn/`。

| Field | Value |
|---|---|
| Severity | LOW |
| Status | open (docs fix / rig fix) |
| Scenario | S5 (Agent A — memory remember+recall) |
| Found | 2026-05-04 |

---

## 観測 (Agent A raw report)

S5 実行前の cleanup コマンド:
```bash
rm -rf .reyn/state .reyn/events
```

実行後、 `history.jsonl` が残存。 前回 session の tool call sequence が
replay されることで、 S5 の fresh run の観測が汚染される。

## 期待との差

dogfood rig は scenario 間で完全に状態をリセットする想定。
`history.jsonl` は session 履歴を保持するため、 state と events だけ
消しても不完全。

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **state 整合性** | history.jsonl が残るため 「初期状態」 観測ができない |
| 他 5 軸 | dogfood rig の問題なので直接影響なし |

## Severity guess

**LOW** — bug ではなく dogfood 手順の問題。 fix は簡単:

```bash
rm -rf .reyn/    # 完全リセット
# または: scenarios.md / prelude.md に「正しいリセット手順」 を明記
```

dogfood rig スクリプト (`scripts/dogfood_run.sh` 等があれば) に
この cleanup を組み込むことを推奨。 batch 3 前に scenarios.md の
「Setup」 節を更新して周知。
