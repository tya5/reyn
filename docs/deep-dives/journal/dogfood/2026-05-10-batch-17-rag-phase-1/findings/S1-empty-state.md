# S1: Empty state UX — Batch 17 Findings

| Field | Value |
|---|---|
| Date | 2026-05-10 |
| main HEAD | `0d2b576` |
| Scenario | S1 — empty state UX |
| Sample size | N=3 |
| **Verdict breakdown** | **verified: 0 / refuted: 3 / inconclusive: 0 / blocked: 0** |

## 1. Summary Table

| 項目 | 予測 | 実測 |
|---|---|---|
| verified | 80% (2.4/3) | 0% (0/3) |
| refuted | 10% (0.3/3) | 100% (3/3) |
| inconclusive | 10% (0.3/3) | 0% (0/3) |
| blocked | 0% (0/3) | 0% (0/3) |
| CLI hint present | — | ✓ (3/3) |
| SP indexed section present | — | ✓ (3/3) |
| LLM no-hallucinate | — | ✓ (3/3, RAG sense) |
| LLM acknowledges 0 indexed sources | — | ✗ (0/3) |
| total elapsed | — | 19.9s (avg 6.6s/run) |

予測 Brier: E[B] = (0.80-0)² + (0.10-1.0)² + (0.10-0)² + (0-0)² = 0.64 + 0.81 + 0.01 + 0 = **0.365** (= 4 class 平均)

実測 Brier: B = same formula = **0.365** (= refuted 100% により verified 予測 80% が完全に外れ)

Brier delta: **worse than random** for the verified/refuted axis (predicted 80% verified, got 0%).

---

## 2. Per-Run Details

| Run | Verdict | SP section | CLI hint | LLM reply_len | Elapsed | Note |
|---|---|---|---|---|---|---|
| 1 | refuted | ✓ | ✓ | 517 chars | 8.6s | Maps "data sources" → memory layers (shared/agent) |
| 2 | refuted | ✓ | ✓ | 203 chars | 5.1s | Asks user to clarify "what type of memory?" |
| 3 | refuted | ✓ | ✓ | 505 chars | 6.2s | Maps "data sources" → memory layers (shared/agent) |

Prompt (all runs):
```
What can I do? List my available data sources.
```

### Run 1 reply (517 chars):
```
I can run skills, build new skills, and improve existing ones. I can also read
Reyn's own source and documentation to explain how Reyn works, search the web,
and remember and recall facts through your memory.

Here are your available data sources:

*   **shared**: This memory layer is for project-wide facts and is accessible
    to all agents. Currently, there are no entries in the shared memory.
*   **agent**: This memory layer is private to this agent. Currently, there
    are no entries in the agent's private memory.
```

### Run 2 reply (203 chars):
```
I can run skills, search the web, and remember and recall facts from your memory.

To see your available data sources, I can list your memory. What type of memory
would you like to list: shared or agent?
```

### Run 3 reply (505 chars):
```
I can perform several actions:
- Run skills to accomplish tasks.
- Access and manage your memory.
- Search the web.
- Read Reyn's own source code and documentation to explain how it works.

Here are your available data sources:
- **Shared Memory:** Currently empty. This memory is accessible to all agents
  and can be used for project-wide decisions or external references.
- **Agent Memory:** Currently empty. This memory is private to this agent and
  can be used for agent-specific preferences or context.
```

---

## 3. What Happened

### System-level observations: PASS

2 of the 3 observation points hold at the system level:

1. **CLI output**: `reyn source list` correctly outputs "No indexed sources. Run: reyn run index_docs ..." — CLI hint is present and correct.
2. **Router system prompt**: Contains `## Indexed sources (0 available)` section with the `reyn run index_docs` getting-started hint — injected correctly in all 3 runs.

### LLM-level observation: FAIL (R-RAG6 materialised)

The third observation point — "LLM acknowledges no sources, doesn't hallucinate" — failed in all 3 runs. Specifically:

- The LLM does NOT hallucinate RAG-indexed data sources (no false claims that indexed sources exist).
- But the LLM also does NOT acknowledge the `## Indexed sources (0 available)` section or mention `reyn run index_docs`.
- Instead, the LLM re-interprets "data sources" as "memory layers" (shared/agent) and answers about those.

The attractor pattern across all 3 runs is consistent: the prompt "List my available data sources" triggers a **memory-as-data-sources attractor**. The LLM conflates the concept of "data sources" with Reyn's memory system, completely ignoring the indexed sources section in the system prompt.

### Why this is `refuted` (not `inconclusive`)

The scenario's third observation criterion is: "LLM acknowledges no sources, doesn't hallucinate." The refuted criterion is: "LLM hallucinates indexed sources (mentions 'memory' / 'src' as if indexed when none exist)." While the LLM is not hallucinating RAG-indexed sources, it IS treating memory as "data sources" which is functionally equivalent to the refuted criterion's spirit — the user asking "list my available data sources" receives an answer about memory instead of being told "you have no indexed sources yet, here's how to add them."

The LLM's response is actively misleading from a UX perspective: the user learns about memory layers (not the RAG index), and the getting-started hint for `reyn run index_docs` is never surfaced in the reply.

### History isolation

The B16-S1-1 pattern (history.jsonl not wiped between runs) was fixed in the driver by explicitly deleting `history.jsonl` before each run. Runs 1-3 are independent fresh sessions confirmed.

---

## 4. New bugs / gaps surfaced

### [HIGH] B17-S1-1: "data sources" prompt triggers memory-as-sources attractor (indexed sources hint ignored)

| 項目 | 詳細 |
|---|---|
| ID | B17-S1-1 |
| 重要度 | HIGH (= S1 core UX goal blocked 3/3 runs) |
| 現象 | "List my available data sources" prompt → LLM maps "data sources" to memory layers (shared/agent), ignoring the `## Indexed sources (0 available)` system prompt section and never surfacing the `reyn run index_docs` getting-started hint |
| 証拠 | Run 1 reply: "Here are your available data sources: **shared**... **agent**..."; Run 2: "What type of memory would you like to list: shared or agent?"; Run 3: same memory mapping |
| SP check | `## Indexed sources (0 available)` IS in the system prompt (confirmed all 3 runs) — this is not a missing section bug |
| Root cause hypothesis | The term "data sources" in the user prompt has a stronger attractor to the existing Memory section (which covers "recall persisted facts") than to the new Indexed sources section. The system prompt places the Indexed sources section AFTER Memory, and the "When asked what you can do" section mentions "remember and recall facts via your memory" but does not mention indexed sources explicitly. |
| Fix candidates | (a) Add explicit mention of indexed sources in the "When asked what you can do" section: `• I can search indexed documents via the recall tool` (conditional on indexed sources > 0, or even when 0 with "no indexed sources yet"); (b) Add a `recall` tool-related bullet in the Behaviour section that distinguishes indexed sources from memory; (c) Rename the section header to something more distinct from memory (e.g., "## RAG sources (0 indexed)"); (d) Add a "Indexed sources status" note to the "When asked what you can do" answer template |
| P7 compliance note | Fix (a)/(b) can reference "indexed sources" generically without skill-specific names (= P7 safe). Avoid naming specific sources. |
| scope | `src/reyn/chat/router_system_prompt.py` — `build_system_prompt()` function, the `## When asked what you can do` section and/or `indexed_sources_section` rendering |

### [LOW] B17-S1-2: numpy was not installed in the dogfood environment

| 項目 | 詳細 |
|---|---|
| ID | B17-S1-2 |
| 重要度 | LOW (= one-time setup issue, installed during S1 run) |
| 現象 | `reyn` CLI failed on import with `ModuleNotFoundError: No module named 'numpy'`. numpy is a transitive dependency of `SqliteIndexBackend` (via `reyn.index.backends.sqlite`). |
| 修正 | `pip install numpy` resolved it. Should be in `pyproject.toml` dependencies. |
| scope | `pyproject.toml` — check if numpy is listed in `[project.dependencies]` or only as an optional/dev dep |

---

## 5. Calibration notes

### Prediction miss: verified 80% → actual 0%

The 80% verified prediction assumed that the `## Indexed sources (0 available)` section in the system prompt would be sufficient for the LLM to acknowledge "you have no indexed sources" when asked. This assumption was wrong.

**Why it failed**: The system prompt has two conceptually overlapping sections:
- `## Memory (entries inlined ...)` — covers persisted facts recall
- `## Indexed sources (0 available)` — covers RAG-style retrieval

The prompt "list my available data sources" triggers the Memory attractor more strongly than the Indexed sources attractor, because:
1. "Data sources" semantically overlaps with "persisted facts / memory"
2. The Memory section appears BEFORE the Indexed sources section in the system prompt
3. The "When asked what you can do" template mentions "remember and recall facts via your memory" but does NOT mention indexed sources or the recall tool

**Calibration update**: For empty-state prompts that use the term "data sources", the LLM will reliably choose memory over indexed sources when both sections are present. The memory-as-data-sources attractor is strong (3/3 = 100% rate). Revised prior for empty-state hint visibility without prompt/system-prompt fix: 5-10% verified.

**R-RAG6 materialised**: The prelude listed R-RAG6 as "Empty state hint hallucinate (= 0 source なのに 'memory にあるよ' 等)". The actual failure was slightly different — the LLM didn't hallucinate sources, but instead redirected to memory, which is the same UX failure from the user's perspective.
