# B7-S3: B4-M1 eval.md path mismatch 解消確認 — observation

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 578bb03 |
| Scenario | S3 (B4-M1 eval.md path fix の e2e 効果) |
| Verdict | **inconclusive** (= chain 未起動、prepare phase 未到達) |

---

## 背景

B4-M1: `eval_builder` が write する eval.md の path と `prepare` phase が read しようとする path に不整合があり、batch 4 で 4 回の failed read を観測。

修正:
- Wave 1 (`0a92db0`): `eval_md_path_for(name)` helper 追加
- Wave 2 (`e6de782`): eval_builder が OS preprocessor 経由で path を解決するよう改修

S3 の観測目的: prepare phase の WAL を分析し、failed read が 0 件に落ちたかを verify。

## Action (S1 と同一 session)

Input: `skill_improver で direct_llm を 1 回 review して改善案を出して`

session: `events/agents/default/chat/2026-05/2026-05-04T183013.jsonl`

## 観測

### prepare phase への到達

```
[Skill Chain]  (0 workflow(s))
```

**prepare phase 未到達**。router が `invoke_skill("skill_improver.direct_llm")` で
失敗し、skill_improver 自体が起動しなかった。prepare phase の WAL events は皆無。

### eval.md path search の trace

```bash
grep '"phase": "prepare"' .reyn/state/wal.jsonl | grep eval.md
→ (empty - WAL entries: 0 for prepare)
```

WAL は `inbox_put` / `inbox_consume` の 2 entries のみ。phase events なし。

### prepare phase の file/read events

```
failed read 数: N/A (prepare 未起動)
total read 試行: N/A
```

## 事前 prediction 評価

scenarios.md の prediction:

```
internal metric (failed read 0 件): 70% verified / 20% inconclusive / 10% refuted
```

実 verdict: **inconclusive** (= prepare 未到達、観測不能)

top probability category = 70% verified → 実 verdict = inconclusive → **prediction miss**

prediction の 20% inconclusive 枠が当たっているが、
その理由は「別 attractor で prepare 未到達」と scenarios.md に記載されており、
今回のケースはまさにそのケース (router LLM が別バグで chain 未起動)。

| 分布 | 実 verdict との対応 |
|---|---|
| 70% verified (path 整合) | 未観測 (inconclusive) |
| 20% inconclusive (別 attractor) | **HIT**: router バグで chain 未起動 |
| 10% refuted (path 不整合残存) | 未観測 |

## verdict 根拠

B4-M1 の path fix (Wave 1 + Wave 2) の e2e 効果は今回 **観測不能**。
router LLM の新バグ (B7-NEW-1) により chain 自体が起動しなかった。
`eval.md path` の read/write は prepare → copy_to_work → run_and_eval と
eval_builder sub-skill が走らない限り観測できない構造。

## 制約

S3 verdict は「inconclusive (blocked by upstream bug)」。
B4-M1 の fix 効果検証は S1 の router LLM fix (B7-NEW-1) 解決後に再実施が必要。

## next action

B7-NEW-1 fix 後に chain を完走させ、prepare phase WAL の eval.md read events を観測。
具体的な check コマンド:
```bash
grep '"phase": "prepare"' .reyn/state/wal.jsonl | grep eval.md
grep '"phase": "prepare"' .reyn/state/wal.jsonl | grep '"status": "error"' | grep eval.md
```
で failed read 0 件を確認する。
