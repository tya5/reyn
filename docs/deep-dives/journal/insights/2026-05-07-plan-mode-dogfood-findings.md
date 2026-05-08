---
title: plan-mode dogfood — 3 bugs found via LLM context analysis discipline
discovered: 2026-05-07
session-context: plan-mode (commit `6b41fd0` 前日 land、 = decompose-and-execute pattern) を初めて dogfood verify した wave。 N=10 での scenario-dependent attractor を 1/10 noise として dismiss しかけて user push back で軌道修正
related-commits:
  - 6b41fd0  # plan-mode initial implementation (= 前日 land、 dogfood 未実施)
  - ea97509  # bug fix 1+2 (_PlanStepHost methods + step recursion guard)
  - 7d0d6a2  # bug fix 3 (plan tool description disambiguation)
related-giveup: []
related-memory: [feedback_per_scenario_attractor_audit, feedback_envelope_layer_fix]
status: stable
---

# Plan-mode dogfood — 3 bugs found via LLM context analysis discipline

## TL;DR

plan tool (= 複雑 query を 2-7 sub-task に decompose して narrow LLM call で
execute する mechanism) を初めて dogfood verify、 **3 件の bug 発見 + fix +
re-verify** で完走。

各 bug は **異なる layer**:
1. **Code-level**: `_PlanStepHost` facade に必須 method 5 件欠落 → 全 step crash
2. **Architecture-level**: plan step recursion guard 不足 → 1 plan が 3x 起動
3. **Description-level**: `step.tools` field の namespace ambiguity → 25% scenario-
   dependent skill-name-in-tools mistake

最も重要な学び: **「N=10 で 1/10 = 10% noise」 を「subprocess 不安定 + minor
LLM mistake」 と dismiss しかけたが、 LLM context 分析で「scenario-dependent な
25% attractor (= "compare" 動詞でのみ発生)」 と判明**。 user の即時 push back
「P3 の llm context 分析すみ？」 で軌道修正、 description fix で 100% に。

## Setup — plan-mode の design

実装場所: `src/reyn/chat/planner.py` (= 前日 `6b41fd0` で land)

flow:
```
user query → router LLM → plan tool emit (with steps_json)
            → execute_plan() → 各 step を child RouterLoop で実行
            → final step's text → outer router → user-facing reply
```

各 step は narrow `_PlanStepHost` facade を介して動く:
- step.tools が指定する subset of tools のみ visible
- step description が user_text として child loop に渡る
- step's captured text が `prior_results` に蓄積、 後続 step の system prompt に inject

## Bug 1: `_PlanStepHost` 必須 method 5 件欠落

### 観測 (= 1st smoke run)

query: `"Read both README.md and CLAUDE.md, then build a side-by-side comparison"`

trace 結果:
```json
{"step_failures": {
  "read_readme":  "AttributeError(\"'_PlanStepHost' object has no attribute 'resolve_model'\")",
  "read_claude":  "AttributeError(\"'_PlanStepHost' object has no attribute 'resolve_model'\")",
  "compare_content": "AttributeError(\"'_PlanStepHost' object has no attribute 'resolve_model'\")"
}}
```

= **3 of 3 steps failed**、 plan が起動するたびに crash。

### 真因

`_PlanStepHost` は `RouterLoopHost` Protocol を満たすべき facade。 RouterLoop
は LLM call 直前に `host.resolve_model(self.router_model)` を呼ぶが、 facade は
この method を `parent` に passthrough していなかった。

加えて以下も欠落:
- `mcp_list_servers` / `mcp_list_tools` / `mcp_call_tool`
- `file_delete`

### Fix (`ea97509`)

5 method を passthrough で追加。 各 step が parent host の経由で resolve / dispatch。

### 教訓

- **facade pattern は Protocol を厳格に満たす必要**。 静的 type check (= mypy)
  では catch されないので、 dogfood でしか発見できない
- `RouterLoopHost` 自体に `Protocol` annotation 付与 + `_PlanStepHost` を
  isinstance check するか、 conformance test を書くのが follow-up 候補

## Bug 2: plan step recursion (= nested plan)

### 観測 (= bug 1 fix 後の re-smoke)

trace 結果: **`plan` tool が同 turn で 3 回 invoke** (= [0], [5], [10])。 各
plan が 3-step plan を emit、 step 内 LLM が更に plan を emit する recursion 開始。
total 20 router pairs。

### 真因

`execute_plan()` は child RouterLoop を構築する時、 `build_tools(...)` を
unconditionally に呼ぶ。 `build_tools` は **`plan` tool を unconditionally に
含める**。 結果、 step LLM の tools= array に `plan` が visible、 LLM が「step
内で更に decompose」 と判断して再帰。

router_loop.py:696 で `available_tool_names=set(self._tool_names) - {"plan"}`
は parse_and_validate_plan の **validation** に使われていただけで、 tools= array
の filter には effect なし。 「no nested plans」 comment は intent であって
implementation ではなかった。

### Fix (`ea97509`)

`RouterLoop.__init__` に `exclude_tools: set[str] | None = None` parameter 追加。
post-build_tools で filter:

```python
if self._exclude_tools:
    tools = [
        t for t in tools
        if t.get("function", {}).get("name") not in self._exclude_tools
    ]
```

`execute_plan` から `exclude_tools={"plan"}` を渡す。

### 教訓

- **「intent の comment」 と「実装」 を混同しない**: code に意図を書いただけで
  動作は伴わない、 必ず test or dogfood で実証
- recursion 系 bug は test では catch しにくい (= mock では recursion 起きない)、
  real LLM dogfood で確認必須

## Bug 3: `step.tools` field の namespace ambiguity

### 観測 (= bug 1+2 fix 後の N=10 dogfood)

| scenario | verified | refuted | 備考 |
|---|---|---|---|
| P1 chitchat | 10/10 | 0 | OK |
| P2 single_tool | 10/10 | 0 | OK |
| **P3 multi_source** | **6/10** | 1 | **3 inconclusive + 1 refuted** |
| P4 synthesis | 10/10 | 0 | OK |
| P5 ambiguous | 10/10 | 0 | OK |

P3 で 1/10 refuted: `"plan fired 1x but no aggregate found"`。

### 私が trap に踏み込みかけた

私は 1/10 を「subprocess 不安定 (3 inconclusive) + LLM 1 mistake」 と dismiss、
「system は fail-fast、 minor follow-up」 と判定。

→ **user push back**: 「P3 の llm context 分析すみ？」

これで軌道修正、 LLM context dump を実施。

### LLM context 分析

P3 run 09 で LLM が emit した plan args:

```json
{
  "id": "compare_content",
  "tools": ["direct_llm"],
  "depends_on": ["read_readme", "read_claude"]
}
```

`direct_llm` は **skill 名** (= `invoke_skill.name` enum の valid 値)、
**top-level tool 名** (= 15 tools の中) には存在しない。

LLM context には 2 つの分離した namespace が見えていた:

| catalog | 例 | LLM が見る場所 |
|---|---|---|
| Top-level tool 名 (15) | `list_skills` / `invoke_skill` / `plan` / `web_search` / `reyn_src_read` 等 | `tools=` array の functional name |
| Skill 名 (10) | `direct_llm` / `eval` / `skill_improver` 等 | `invoke_skill.name` parameter の enum 制約 |

= 完全 disjoint。 LLM は categorical 混同で `direct_llm` (= skill 名) を
`step.tools` (= top-level tool 名要求) に置いた。

`step.tools` description は:

> "tools: names of tools this step needs (subset of available catalog;
> [] for narration-only steps)"

「subset of available catalog」 で **どっちの catalog か disambiguate なし**。
LLM が `invoke_skill.name` enum を「a catalog」 として選んだ。

### 再現性分析 (= scenario-dependent attractor)

| scenario | plan emit 数 | skill-in-tools mistake | 違い |
|---|---|---|---|
| **P3** ("compare" 動詞) | 4 | **1 (25%)** | 「compare」 → tool 必要と LLM 判断 |
| **P4** ("synthesise" 動詞) | 10 | **0 (0%)** | 「synthesise」 → LLM 自前 OK と認識 |

= **動詞依存の rate variance**。 P4 単独では 0% で「healthy」 に見えるが、
P3 では 25%。 1/40 の overall rate (= 50 runs ÷ 40 plan emits の中で 1 mistake)
として dismiss すると、 25% scenario-dependent attractor を masking する。

### Fix (`7d0d6a2`)

description を 3 軸 disambiguate:

```
tools: list of TOP-LEVEL tool names this step calls (e.g. "reyn_src_read",
"web_search", "invoke_skill"). Use [] for steps that just synthesise /
compare / summarise from prior step outputs — the step's LLM does that
natively without any tool. To run a skill, use ["invoke_skill"], NOT the
skill's name.
```

example も更新:
```json
{"id": "s2", "description": "compare and summarise for user",
 "tools": [], "depends_on": ["s1"]}
```

= 「`compare` 動詞でも tools=[] が正解」 を example で明示。

### Re-verify (= post fix)

| scenario | before | after | Δ |
|---|---|---|---|
| P3 multi_source | 6/10 | **10/10** | +40pp ✓ |
| P1/P2 sanity | 100% / 100% | 100% / 100% | 0 (= no over-firing regression) |

skill-in-tools mistake **完全消滅**。

## メタ教訓 — 「LLM context 分析」 discipline の再強調

### Trap pattern

「N=10 で 1/10 = 10% noise」 を以下で dismiss するのは危険:
- subprocess 不安定 (= infra 由来) と分類
- 「system は fail-fast、 graceful」 と判定
- 「minor LLM mistake、 follow-up 候補」 と framing

これらが揃っても **scenario-dependent な 25% attractor を masking してる**
可能性は残る。

### Discipline 再掲

attractor 疑い (= 期待外れの 1 件以上) を見たら:

1. **LLM context dump 必須**: scenario の trace を full inspect (= req messages
   + tools= + response の全 field)
2. **再現性分析 (= scenario 跨ぎ)**: 同 attractor が他 scenario でも起こるか、
   起こらないか。 起こらないなら scenario-dependent (= 動詞 / 構造 / 文体 で変わる)
3. **最小パス探索 (= mutation isolation)**: 何を変えれば消えるか、 description
   level / schema level / envelope level のどこに介入すべきか

これは Wave A revert wave で確立した discipline と同型、 ただし plan-mode では
「rate variance が scenario-dependent」 という 別軸の trap が出現。

### 関連先行 trap (= 同 session 内で経験)

- Wave A 削除 attempt: SP 圧縮で W1 -40pp regression、 真因は G12 Pattern E
- W5 G12 attractor: programmatic 50% vs subprocess 100% で経路依存性が露呈

これらと plan-mode の bug 3 は **「1 metric 見ただけで結論しない」** で共通。

## Final dogfood state (= post all 3 fixes)

5 scenarios × N=10 = 50 runs:
- verified: 47
- inconclusive (= subprocess infra): 3
- refuted: **0** (= 全 LLM-side bug 解消)

| scenario | verified | 動作 |
|---|---|---|
| P1 chitchat | 10/10 | plan 起動せず |
| P2 single_tool | 10/10 | web_search 1 turn |
| P3 multi_source | 10/10 | plan 起動 + 3 step + final synthesis |
| P4 synthesis | 10/10 | plan 起動 + 3 step + final synthesis |
| P5 ambiguous | 10/10 | inline info で回答 |

## References

### Same session insights
- [envelope-layer attractor fix + mutation isolation methodology](2026-05-07-envelope-layer-attractor-fix.md)
- [industry tool discovery patterns survey](2026-05-07-industry-tool-discovery-survey.md)
- [category-only SP catalog landing](2026-05-07-category-only-catalog-landing.md)

### Plan-mode design references
- `src/reyn/chat/planner.py` (= execute_plan + _PlanStepHost facade)
- `src/reyn/chat/router_loop.py` (= exclude_tools parameter at line ~258)
- `src/reyn/chat/router_tools.py` (= plan tool definition + description)

### Commits
- `6b41fd0` plan-mode initial implementation (= 前日 land、 dogfood 未実施で landed)
- `ea97509` plan-mode bug fix 1+2 (_PlanStepHost methods + step recursion)
- `7d0d6a2` plan-mode bug fix 3 (description disambiguation)
