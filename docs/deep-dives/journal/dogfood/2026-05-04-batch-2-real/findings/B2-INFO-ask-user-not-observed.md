# B2-INFO: ask_user IR op の e2e、 S4 では観測不能

> 一行で: S4 で ask_user 経路を観測しようとしたが、 router が skill を invoke する
> 前に pre-skill clarification を行うため、 ask_user IR op が発火しなかった。
> バグではなく観測設計の問題。

| Field | Value |
|---|---|
| Severity | INFO (not a bug) |
| Status | batch 3 で再設計 |
| Scenario | S4 (Agent B — skill + ask_user) |
| Found | 2026-05-04 |

---

## 観測 (Agent B raw report)

User input: `"先週書いた company の Q3 report を読んで要約して"` (vague path)。

期待した挙動:
1. router が `read_local_files` を invoke
2. skill phase 内で LLM が `ask_user` IR op を発行
3. CUI に clarifying question が表示される

実際の挙動:
```
agent> どのcompanyのQ3レポートについて知りたいですか？ また、レポートはどこに保存されていますか？
```

WAL: `inbox_put(2) inbox_consume(2)` のみ。 `skill_started` なし、
`intervention_dispatched` なし。

## 何が起きたか

router LLM が「path が不明なので skill を invoke する前に user に確認」
という判断を行い、 pre-skill clarification として直接 reply した。
これは router として合理的な動作。

ask_user IR op は「skill が動いている間に」 発行されるもので、
skill が起動しない前提では観測できない。

## batch 3 に向けた再設計

ask_user e2e を観測するには:
- user がスキル名を **明示的に指定** する (router が invoke する確率を上げる)
- スキル内部で path が曖昧になる状況を作る

推奨 S4-v2 シナリオ:
```
"read_local_files skill を使って、 このディレクトリにある report.md を読んで要約して"
# (report.md は実在しない → skill の decide_files phase が ask_user を発行する可能性)
```

## Severity guess

**INFO** — 観測設計の問題であり、 ask_user 機能自体の不具合ではない。
Tier 2 test では ask_user IR op の dispatch / resolution は既に pin 済み
(scenarios.md の defer 欄参照)。 e2e 観測は batch 3 で再挑戦。
