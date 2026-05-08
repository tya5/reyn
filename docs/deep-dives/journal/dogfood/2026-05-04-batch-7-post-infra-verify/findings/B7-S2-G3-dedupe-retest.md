# B7-S2: G3 dedupe (B5-M1) post-fix retest — observation

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 578bb03 |
| Scenario | S2 (B5-M1 G3 dedupe 効果検証) |
| Verdict | **verified (partial)** |

---

## 背景

B5-M1: router が同一 `invoke_skill(skill_improver)` を 1 LLM call から 3 件同時発行 (並列 invoke)。
G3 dedupe fix (`9798372`) で F5 sync 拡張が landing 済。
batch 6 (B6-S3) でも並列起動 3 件が再現したが G3 dedupe の効果は未 verify。
batch 7 では S1 と同一 session で G3 dedupe 動作を観測。

## Action (S1 と同一 session)

Input: `skill_improver で direct_llm を 1 回 review して改善案を出して`

session: `/Users/yasudatetsuya/.../worktrees/agent-a1fff477e84c63075/.reyn/events/agents/default/chat/2026-05/2026-05-04T183013.jsonl`

## 観測

### 並列 invoke 検出

```bash
grep '"tool_call_deduped"' .reyn/events/.../2026-05-04T183013.jsonl
```

```json
{
  "type": "tool_call_deduped",
  "timestamp": "2026-05-04T18:30:44.643374+09:00",
  "data": {
    "name": "invoke_skill",
    "chain_id": "fb323e75a85f422fbc622b1c8764f7e1",
    "reason": "duplicate_invoke_skill_in_round"
  }
}
```

**tool_call_deduped イベント: 1 件** (= G3 dedupe が発火)

### tool_called 件数

```
tool_called (invoke_skill): 2 件
  - 1件目: {name: "skill_improver.direct_llm", args_hash: "8366a209ec481f77"}
  - 2件目: {name: "skill_improver.direct_llm", args_hash: "8366a209ec481f77"}  ← 同 hash
```

LLM が同一 args_hash の invoke_skill を 2 件発行し、G3 dedupe が 1 件を deduped。
結果: 2 件発行 → 1 件 deduped → 実行 1 件。

ただし、今回の invoke_skill の `name` が `skill_improver.direct_llm` (不正な dot-notation) のため、
実行 1 件も `ValueError` で失敗。skill chain は未起動。

### skill_runs 件数

```bash
ls .reyn/state/skill_runs/ | wc -l  → ディレクトリ未作成 (skill 未起動)
```

### tokens / LLM call 数

```
Total: $0.000230  |  1,730 tokens  |  1 calls (router)
```

batch 5 比較: 333k tokens / 51 calls (= 3 並列完走)
batch 7 今回: 1,730 tokens / 1 call (= chain 未起動)

## 事前 prediction 評価

scenarios.md の prediction:

```
internal metric (並列 + dedupe 成功): 50% verified / 30% inconclusive / 20% refuted
```

実観測:

| 観測項目 | 結果 |
|---|---|
| 並列 invoke 発生 | yes (2 件発行、1 件 deduped) |
| dedupe 発火 | yes (tool_call_deduped event 1 件) |
| skill chain 起動 | no (不正 skill 名で失敗) |

top probability category = 50% verified。実 verdict:

- G3 dedupe の発火メカニズム自体は **verified** (= deduped event 観測)
- 並列発行数が batch 5/6 の 3 件から **2 件に減少** (理由不明: wording 変化? LLM ばらつき?)
- dedupe 後の skill 名が不正 → chain 未起動という secondary failure あり

**verdict: verified (partial)**
- G3 dedupe は機能している (duplicate_invoke_skill_in_round reason で 1 件削減)
- ただし「並列 invoke → dedupe → skill chain 完走」の e2e は未到達
- 並列 invoke 数の 3→2 への変化は observation として記録 (LLM ばらつきの可能性)

prediction hit/miss:
- top category (50% verified) → verdict = verified (partial) → **HIT (partial)**

## 重要 evidence

```json
{
  "type": "tool_call_deduped",
  "timestamp": "2026-05-04T18:30:44.643374+09:00",
  "data": {
    "name": "invoke_skill",
    "chain_id": "fb323e75a85f422fbc622b1c8764f7e1",
    "reason": "duplicate_invoke_skill_in_round"
  }
}
```

## G3 resolved 化の判断

G3 dedupe の **発火メカニズム** は e2e で動作を確認できた。
ただし「完走した skill chain の並列 invoke が dedupe される」ケースはまだ未観測
(= 今回は chain 未起動のため)。G3 を fully resolved にするには、chain が完走する
セッションでの `tool_call_deduped` + skill_run 数 1 の確認が必要。

**判定**: G3 は `verified (dedupe 発火)` だが `resolved` には不十分。要追観測。

## next action

S1 の router LLM fix (B7-NEW-1) 後に、同 input で再実行して:
- skill chain が起動するか
- 並列 invoke 数と dedupe 後の skill_run 数が 1 であるか
を確認する。
