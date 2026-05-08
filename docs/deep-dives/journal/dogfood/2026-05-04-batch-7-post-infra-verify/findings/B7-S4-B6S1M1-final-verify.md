# B7-S4: B6-S1-M1 仮説 (a) 実 LLM final verify — observation

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 578bb03 |
| Scenario | S4 (copy_to_work LLM の validation.ok 参照 final verify) |
| Verdict | **inconclusive** (= copy_to_work phase 未到達) |

---

## 背景

B6-S1-M1 仮説 (a):
- `copy_to_work` preprocessor の validation 結果フィールド名が `data._validation` (underscore prefix) だと
  LLM が internal field と解釈して context として無視するという仮説。
- `3cf7412` で `data.validation` に rename 済み。
- `9763ecf` で Tier 3 LLMReplay test が追加され behavioral pin 済 (= verified 間接的)。
- batch 6 dogfood retest (B6-S1-M1 仮説 a retest) は infra bug 2 件で inconclusive。

batch 7 では infra fix 2 件 (`07ee851` / `f666acb`) landing 後に copy_to_work phase に
到達できる前提で、**実 weak LLM が `data.validation.ok` を transparent に参照するか**
を直接 verify する目的。

## Action (S1 と同一 session)

Input: `skill_improver で direct_llm を 1 回 review して改善案を出して`

session: `events/agents/default/chat/2026-05/2026-05-04T183013.jsonl`

## 観測

### copy_to_work phase への到達

```
[Skill Chain]  (0 workflow(s))
```

**copy_to_work phase 未到達**。router LLM が `invoke_skill("skill_improver.direct_llm")` で
失敗し、skill_improver 自体が起動しなかった (prepare phase も未到達)。

### WAL 直接 grep

```bash
grep '"phase": "copy_to_work"' .reyn/state/wal.jsonl
→ (empty)
```

```bash
grep 'validation.ok\|validation_ok\|validation:' .reyn/state/wal.jsonl
→ (empty)
```

WAL: 2 entries のみ (inbox_put / inbox_consume)。copy_to_work events なし。

### copy_to_work LLM response

```
LLM response: N/A (phase 未起動)
validation.ok 参照: 観測不能
```

## Tier 3 との比較

batch 7 S4 の目的は「Tier 3 LLMReplay で pin した挙動 (`9763ecf`) が実 weak LLM でも再現するか」の確認だった。

| 検証手段 | 状態 |
|---|---|
| Tier 3 LLMReplay test (`9763ecf`) | passed (= `775 passed / 2 xfailed` に含まれる) |
| 実 LLM e2e verify (batch 7 S4) | **inconclusive** (chain 未到達) |

Tier 3 は過去の fixture (= capture された LLM 応答) で behavioral pin しているため
「実 weak LLM での挙動」は別途確認が必要。batch 7 ではその確認が行えなかった。

## 事前 prediction 評価

scenarios.md の prediction:

```
internal metric (LLM が validation.ok を response 内で参照): 60% verified / 30% inconclusive / 10% refuted
```

実 verdict: **inconclusive** (= copy_to_work 未到達)

top probability category = 60% verified → 実 verdict = inconclusive → **prediction miss**

| 分布 | 実 verdict との対応 |
|---|---|
| 60% verified | 未観測 |
| 30% inconclusive | **HIT**: copy_to_work 未到達 (ただし理由は upstream routing bug) |
| 10% refuted | 未観測 |

30% inconclusive が当たったが、scenarios.md の inconclusive 想定理由は
「参照せず黙々と transition」であり、今回の実際の理由 (upstream routing bug) とは異なる。
実質的には prediction から外れた形の inconclusive。

## verdict 根拠

copy_to_work phase に到達するためには:
1. router LLM が正しい `invoke_skill("skill_improver")` を呼ぶ
2. prepare phase が完走する
3. copy_to_work phase が preprocessor 経由で起動する

今回は step 1 でブロックされ、B6-S1-M1 仮説 (a) の実 LLM 確認は **一切行えなかった**。

B6 dogfood retest も infra bug で inconclusive → batch 7 も router bug で inconclusive。
仮説 (a) の実 LLM 確認は引き続き pending 状態。

## next action

B7-NEW-1 (router LLM dot-notation 誤解釈) fix 後に再実行。copy_to_work LLM response を WAL から抽出し:

```bash
grep '"phase": "copy_to_work"' .reyn/state/wal.jsonl | grep llm_response_received
grep '"phase": "copy_to_work"' .reyn/state/wal.jsonl | grep -E "validation\.ok|validation_ok"
```

で `validation.ok` 参照の有無を直接確認する。
Tier 3 test が passed なので仮説 (a) は高確率で正しいが、実 weak LLM での確認が pending 継続。
