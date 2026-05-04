# B4-M1 [MED]: eval.md の置き場所、 書き手と読み手で食い違う

> 一行で: `eval_builder` が `reyn/local/<slug>/eval.md` に書き、 `prepare`
> phase は `<target_dsl_root>/eval.md` を先に探す。 4 回 failed read 後に
> 正しい path に辿り着く非効率。

| Field | Value |
|---|---|
| Severity | MED |
| Status | open |
| Scenario | S3 (skill_improver nested chain) |
| Found | 2026-05-04 |
| Raw observation | [B4-S3-skill-improver-nested.md](B4-S3-skill-improver-nested.md) |

---

## 観測

skill_improver chain の `prepare` phase が eval.md を探す挙動を WAL で trace:

```
prepare phase:
  file/read <target_dsl_root>/eval.md         → ENOENT (failed)
  file/read <target_dsl_root>/eval/eval.md    → ENOENT (failed)
  file/read reyn/eval/eval.md                 → ENOENT (failed)
  file/read reyn/local/eval/eval.md           → ENOENT (failed)
  file/read reyn/local/<target_slug>/eval.md  → ok
```

eval.md は **5 回目** で発見された。 LLM が候補 path を試行錯誤、 4 回の failed
read は cost 無駄。 さらに events log が `read_failed` で汚染される
(= forensic value 低下)。

`eval_builder` skill (= eval.md の書き手) の output path を確認:

```
eval_builder write target: reyn/local/<target_slug>/eval.md  (= 最後に hit したやつ)
```

→ 書き手と読み手の path 期待値が一致していない。

## つまり何が起きたか

`eval_builder` と `prepare` phase の設計時点で **eval.md の置き場所の
convention が確立されていなかった**。 `prepare` の instruction は「eval.md を
探せ」 だけで、 「どこに居るか」 を 1 source of truth として明示していない。
LLM が「ありそうな場所」 を順に試す attractor。

これは:

- **設計時 contract の漏れ**: skill 間で artifact をやり取りする path 規約
  (= 「eval.md は `reyn/local/<slug>/` に置く」) が docs / ADR で明文化
  されていない
- **prepare の wording**: 「eval.md を `<target_dsl_root>` で探せ」 が
  instruction の primary fallback path、 これが eval_builder の write path と
  一致していない

batch 5 でも同 attractor 再現可能と推測 (= 再 verify 未)。

## 影響

- skill_improver chain で 4 turn 分の cost 無駄
- events log の `read_failed` event が真の error と混同される (= debug 効率
  低下)
- skill 間 artifact 規約が定まっていない問題が他 skill にも潜在 risk

## 修正候補 (open、 未着手)

option A (recommended): **path convention を docs で formalize**
- `docs/en/concepts/skill-artifact-paths.md` 等で「skill 間で渡す artifact は
  `reyn/local/<slug>/<artifact>.md` に置く」 を明文化
- `eval_builder` instruction と `prepare` instruction の両方で同 path を
  primary candidate として記述

option B: **`prepare` の path search 順序を逆転**
- `reyn/local/<slug>/eval.md` を最初の候補にする
- 既存 fallback (= `<target_dsl_root>/eval.md` 等) は後段に維持

option C: **path を引数化**
- skill_improver 側が input artifact の field として `eval_md_path` を渡す
- LLM が探さなくて済む

→ option A + option B の組み合わせが healthy。 batch 6 以降の MED wave で扱う。

## 後続 candidate

- batch 5+ で skill_improver 経由で eval.md path mismatch が再現するか確認
- 他 skill 間 artifact yang も同様の path 不整合がないか audit
- ADR で「skill 間 artifact path convention」 を formalize

## 教訓

1. **artifact path 規約は docs で 1 source of truth 化**: skill 間で
   artifact を渡す場合、 path を「ありそうな場所を試す」 で済ませず convention
   を ADR / docs に固定する
2. **書き手と読み手の wording を整合させる**: 同じ artifact について
   `eval_builder` (writer) と `prepare` (reader) の instruction が独立に
   書かれていると path がずれる。 cross-skill review が必要
3. **`read_failed` event は真 error と区別したい**: 「期待 fallback の順次
   探索」 と「予期しない file 不在」 を event field で区別できると debug
   効率が上がる (= observability 改善候補)
