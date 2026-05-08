# F1 [HIGH]: chat に話しかけたかったのに、 chat が起動を拒否した話

> 一行で: reyn chat を起動した瞬間 `AttributeError`。 「Did you mean」 まで
> 親切な Python だが起動はしない。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** at `f5b3281` |
| Scenario | scenario 1 |
| Found | 2026-05-04 |

---

## 観測

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

## 原因

`PR-refactor-session-1` (commit `41ec4cb`、 wave 1C) で `ChatSession` から
`InterventionRegistry` service を抽出し、 active intervention queue を
`session._interventions` に移動した。 ところが `chat/registry.py:attach()`
の最終 block で旧 attribute `_active_interventions` を参照したまま、
誰にも気づかれず main に乗っかっていた。

PR-refactor-session-1 wave 2 で「session-level Tier 2 invariants」 を 10 件
追加したが、 **attach 後の pending intervention 再 announce 経路は test
範囲外** だった。 TDD で「赤 → 緑 → refactor」 を回したつもりが、 そもそも
「赤」 になる test が無かった経路で regression が滑り込んだ事例。

## 修正

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

## 教訓

- TDD の「赤 → 緑」 で test を書く前に、 **どの経路を pin するか** を意識的に
  enumerate しないと、 test policy 守ってても regression は普通に起きる
- service 抽出 refactor で「呼び出し側の参照を全部移行できたか」 を機械的
  に確認する仕組み (= 静的解析 / grep audit) が欲しい。 案: 抽出時に
  「旧 attribute を deprecated property に格下げして warning 出す」
- coverage gap として Wave B で attach 経路 + pending intervention
  combination の Tier 2 を追加
