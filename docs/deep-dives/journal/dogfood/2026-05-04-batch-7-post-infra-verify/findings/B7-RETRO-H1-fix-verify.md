# B7-RETRO-H1 fix verify: router enum effective via --patch replay

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | `eeb8ed9` |
| Fix commit | `9ee6ae1` |
| Verdict | **fix effective** |
| Method | `--patch` replay on pre-fix dump (old behavior reproduced then suppressed) |

---

## Setup

Dump file: `.reyn/llm_trace_h1.jsonl` from worktree `agent-a13b00459606cc351`
(pre-fix dogfood session — router was running the OLD code without enum constraint).

Router request chosen for replay:
```
request_id: 6eb3e6d2-3287-477d-8e9a-fa7db398412f
caller:     router
model:      gemini-2.5-flash-lite
msgs:       4  (system + 3× same user turn: "skill_improver で direct_llm を 1 回 review して改善案を出して")
tools:      11
```

Old `invoke_skill` schema (pre-fix):
```json
{
  "name": {
    "type": "string",
    "description": "Skill name as listed by list_skills."
  }
}
```
No enum. Old system prompt showed only `general (10)` (category count, no skill names).

---

## Action

### Condition A — Baseline (no patch, old behavior)

```bash
LITELLM_API_BASE=http://localhost:4000 OPENAI_API_KEY=dummy \
  python scripts/llm_replay.py 6eb3e6d2-3287-477d-8e9a-fa7db398412f \
  --trace .reyn/llm_trace_h1.jsonl \
  --n 10 --output-format json
```

### Condition B — Patched (fix applied: enum + flat list)

```bash
LITELLM_API_BASE=http://localhost:4000 OPENAI_API_KEY=dummy \
  python scripts/llm_replay.py 6eb3e6d2-3287-477d-8e9a-fa7db398412f \
  --trace .reyn/llm_trace_h1.jsonl \
  --patch 'tools[6].function.parameters.properties.name.enum=["direct_llm","eval","eval_builder","judge_phase","mcp_search","read_local_files","skill_builder","skill_importer","skill_improver","word_stats_demo"]' \
  --patch 'tools[6].function.parameters.properties.name.description=Skill name — choose exactly one from the enum (verbatim, no dots or slashes).' \
  --patch 'tools[6].function.description=Run a skill from the registered list. The name parameter MUST be one of the skills listed in the system prompt Available skills section, used verbatim (no dots, no slashes, no namespace prefixes). If unsure of input format, call describe_skill first.' \
  --patch 'messages[0].content+=\n\n## Available skills (10) — use these exact names with invoke_skill\n  - direct_llm: ...\n  - eval: ...\n  - eval_builder: ...\n  - judge_phase: ...\n  - mcp_search: ...\n  - read_local_files: ...\n  - skill_builder: ...\n  - skill_importer: ...\n  - skill_improver: ...\n  - word_stats_demo: ...' \
  --n 10 --output-format json
```

(Patch index 6 = `invoke_skill` tool; confirmed by `llm-tools-schema` output.)

---

## 観測

### Condition A: Baseline N=10 (no patch)

| Metric | Value |
|---|---|
| Total invoke_skill calls | 28 (10 runs × ~2.8 avg) |
| Finish reason | 10/10 tool_calls |
| Avg prompt_tokens | 1,541 |

**Skill name distribution:**

| Name | Count | Status |
|---|---|---|
| `skill_improver` | 12 | CORRECT |
| `skill_improver.review_skill` | 9 | HALLUCINATE |
| `skill_improver.review_llm` | 6 | HALLUCINATE |
| `skill_improver.review` | 1 | HALLUCINATE |

**Hallucination rate: 16/28 = 57%**

### Condition B: Patched N=10 (enum + flat list)

| Metric | Value |
|---|---|
| Total invoke_skill calls | 12 (10 runs × 1.2 avg) |
| Finish reason | 4/10 tool_calls, 6/10 stop |
| Avg prompt_tokens | 1,900 (+359 from flat list) |

**Skill name distribution:**

| Name | Count | Status |
|---|---|---|
| `skill_improver` | 12 | CORRECT |

**Hallucination rate: 0/12 = 0%**

### 直接比較 table

| | Condition A (baseline) | Condition B (patched) |
|---|---|---|
| invoke_skill calls | 28 | 12 |
| Hallucinate count | 16 | 0 |
| Hallucinate rate | **57%** | **0%** |
| finish=tool_calls | 10/10 | 4/10 |
| finish=stop (text reply) | 0/10 | 6/10 |

---

## verdict 根拠

- Condition A (旧: enum なし + カテゴリ集計のみ) で N=10 run の 57% が hallucinated skill 名を生成。
  観測された hallucinated names: `skill_improver.review_skill`, `skill_improver.review_llm`, `skill_improver.review`。
  これは RETRO-H1 が特定した dot-notation hallucination パターンの再現。

- Condition B (fix 後: enum + flat list patch 適用) で N=10 run で hallucination ゼロ (0/12)。
  enum 制約がスキーマ層で dot-notation を物理的に弾くため、モデルが任意文字列を生成できない。

- **Fix commit `9ee6ae1` の効果は確認済み**: enum injection により hallucination は 57% → 0% に消失。

---

## 残懸念点

1. **finish=stop 増加 (6/10)**: patched condition でモデルが tool_call ではなくテキスト返答を選ぶケースが増えた。
   これは flat list を追加したことによる system prompt の変化 (+ messages context の構成変化) が影響している可能性。
   実際の本番動作 (fix 後の reyn router) では flat list の位置・wording が異なるため、この stop 増加が実 e2e でも起きるかは別途確認が必要。
   経路 2 (fresh dogfood with fix) での e2e 確認が推奨。

2. **N=10 サンプル数**: 確率的挙動の正確な測定には N が小さい。baseline の hallucination 率 57% は
   モデルのセッション状態や context 内容により変動しうる。より大きな N での統計的確認が望ましい。

3. **patch の完全一致**: `--patch` で適用した schema/prompt は fix commit の実装に近似しているが
   完全一致ではない (wording, ordering, categories section の残存など)。
   完全一致の確認は経路 2 (fresh dogfood) で担保する。

4. **`describe_skill` 呼び出し回避**: patched condition でモデルが `describe_skill` を経由せずに
   直接 `invoke_skill` を呼ぶパターンと、呼ばないパターンが混在。G12 attractor との関係は別 wave。
