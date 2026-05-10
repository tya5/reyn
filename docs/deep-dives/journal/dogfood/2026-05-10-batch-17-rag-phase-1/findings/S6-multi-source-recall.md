# S6: Multi-source recall — Finding

**Batch**: 17 (2026-05-10)
**Scenario**: S6 — Multi-source recall
**N**: 5
**Verdict**: **blocked** (5/5)

---

## TL;DR

All 5 runs **blocked** due to infrastructure bug B17-S6-1: `recall` and
`drop_source` are registered in the unified `ToolRegistry` but are **never
added to `build_tools()`** in `src/reyn/chat/router_tools.py`. The LLM
receives a system prompt stating "Use the `recall` tool with
`sources=[<name>, ...]` to search" but the `recall` function is absent from
the function-calling catalog. This triggers the empty-stop attractor (R-RAG1)
in all 5 shots — the LLM produces 0-char replies.

Multi-source rate measured: **0/5** (unmeasurable due to blocking bug).

---

## Setup

Per-shot setup (fresh workspace each shot, N=5):

1. Empty `.reyn/` in a temp workspace.
2. `register_fake_embedding_provider()` + `write_dogfood_reyn_yaml(workspace)`.
3. `reyn agent new default`.
4. Seed 2 sources via `write_index_directly()`:
   - `reyn_docs`: 6 chunks from doc content (recall tool, ChunkMetadata,
     SourceManifest, EmbeddingProvider, IndexBackend, index_docs skill).
   - `reyn_src`: 6 chunks from source code (handle_recall, handle_embed,
     handle_index_query, handle_index_write, handle_index_drop, imports).
5. Verify: `reyn source list` showed both sources with `chunk_count=6` each
   — confirmed OK in all 5 shots.
6. Start `reyn web` on a fresh port (each shot uses an isolated port).
7. Send via A2A `message/send`: `"How is recall implemented? I want to
   understand both the design and the actual code."`

---

## Preflight finding (structural, not LLM-behavior)

```
[preflight] recall in build_tools: False
[preflight] drop_source in build_tools: False
[preflight] tool catalog: [
  'list_skills', 'describe_skill', 'list_agents', 'describe_agent',
  'list_memory', 'read_memory_body', 'invoke_skill', 'delegate_to_agent',
  'remember_shared', 'remember_agent', 'forget_memory',
  'web_search', 'plan', 'reyn_src_list', 'reyn_src_read'
]
```

The LLM sees 15 tools. `recall` and `drop_source` are not among them.

---

## Per-run results

| Run | verdict | recall_called | sources_arg | notes |
|-----|---------|---------------|-------------|-------|
| N=1 | blocked | False | None | R-RAG1: empty reply (0 chars) |
| N=2 | blocked | False | None | R-RAG1: empty reply (0 chars) |
| N=3 | blocked | False | None | R-RAG1: empty reply (0 chars) |
| N=4 | blocked | False | None | R-RAG1: empty reply (0 chars) |
| N=5 | blocked | False | None | R-RAG1: empty reply (0 chars) |

All 5 shots: HTTP 200 from A2A, but reply=0 chars. No tool_called events
beyond `user_message_received`. LLM trace confirms:
- System prompt contains "Indexed sources (2 available)" section.
- System prompt instructs: "Use the `recall` tool with `sources=[<name>, ...]`
  to search."
- `recall` absent from the `tools=` array sent to LiteLLM.
- LLM response: content="" / finish_reason=stop / no tool_calls.
  (Response not captured in trace — empty-stop path exits before trace write.)

---

## Verdict breakdown

| verdict | count | rate |
|---------|-------|------|
| verified | 0 | 0% |
| refuted | 0 | 0% |
| inconclusive | 0 | 0% |
| **blocked** | **5** | **100%** |

Multi-source rate (verified / N): **0/5 = 0%**

---

## Brier score

Predicted: `verified=30% / refuted=50% / inconclusive=15% / blocked=5%`
Actual: `blocked=100%` (all others = 0%)

Brier = mean((p_i − o_i)²) across 4 outcomes:
= ((0.30−0)² + (0.50−0)² + (0.15−0)² + (0.05−1.0)²) / 4
= (0.09 + 0.25 + 0.0225 + 0.9025) / 4
= **0.3163**

High Brier reflects completely wrong prediction (predicted refuted-dominant,
got blocked-only). Prediction was based on R-RAG1 / R-RAG5 LLM-behavior
attractors; actual failure was infrastructure.

---

## Root cause analysis

### B17-S6-1 (CRITICAL) — `recall` and `drop_source` missing from `build_tools()`

**File**: `src/reyn/chat/router_tools.py` — `build_tools()` function.

`build_tools()` constructs the `tools=` array for LiteLLM by explicitly
adding individual ToolSpecs for each group (A1–A6, B1–B5, C1–C4, E1–E2, G,
F1–F2, D1–D3). Commit `1e6f153` (feat: recall + drop_source ToolDefinitions)
registered `RECALL` and `DROP_SOURCE` in `get_default_registry()` and in
`tools/__init__.py`, but did **not** add a section to `build_tools()` for the
RAG group.

The unified registry (`_REGISTRY_DISPATCH_TOOLS` frozenset in `router_loop.py`)
also excludes `recall` and `drop_source`, so even if the LLM somehow sent a
`recall` function call, the dispatch layer would return
`{"error": "unhandled tool: recall"}`.

**Evidence**:
```python
# src/reyn/chat/router_loop.py — _REGISTRY_DISPATCH_TOOLS (lines 724–763)
_REGISTRY_DISPATCH_TOOLS: frozenset[str] = frozenset({
    "list_skills", "describe_skill", "list_agents", "describe_agent",
    "delegate_to_agent", "plan",
    "reyn_src_list", "reyn_src_read",
    "web_search", "web_fetch",
    "read_file", "write_file", "delete_file", "list_directory",
    "invoke_skill",
    "list_mcp_servers", "list_mcp_tools", "call_mcp_tool",
    "list_memory", "read_memory_body",
    "remember_shared", "remember_agent", "forget_memory",
    # ← "recall" and "drop_source" are absent
})
```

**Impact**:
- `recall` tool: structurally broken for router use in all sessions with
  indexed sources. System prompt tells LLM to use the tool; LLM cannot invoke it.
- `drop_source` tool: same — never reachable from router context.
- S5 (recall via chat) also blocked by same bug (not confirmed here but implied).
- S8 (drop_source via chat) also blocked by same bug.

**Fix required** (two parts):
1. Add RAG group to `build_tools()` in `src/reyn/chat/router_tools.py`:
   ```python
   # ── H. RAG tools (conditional — indexed sources available) ──────────────
   _recall_def = _registry.lookup("recall")
   if _recall_def is not None and _recall_def.gates.router == "allow":
       # Include when sources are available (or always — tool description
       # guides LLM to check system prompt first)
       ...
   _drop_source_def = _registry.lookup("drop_source")
   if _drop_source_def is not None and _drop_source_def.gates.router == "allow":
       ...
   ```
2. Add `"recall"` and `"drop_source"` to `_REGISTRY_DISPATCH_TOOLS` in
   `src/reyn/chat/router_loop.py`.

---

## Secondary observation: empty-stop attractor (R-RAG1)

When the LLM sees "Use the `recall` tool" in the system prompt but `recall`
is absent from `tools=`, it produces an empty reply (finish_reason=stop,
content="") rather than using available tools (e.g., `reyn_src_read`) or
replying in text. This is the same P-b verbosity / instruction-conflict
attractor seen in batch 14. The system prompt–tool catalog mismatch causes
the LLM to stall.

This is a secondary symptom of B17-S6-1, not a separate LLM-behavior issue.

---

## Carry-over

- **B17-S6-1** (CRITICAL): fix in current batch wave before re-running S5/S6/S8.
- Re-run S6 after fix to measure actual multi-source vs single-source rate
  (R-RAG5 attractor measurement deferred to that retest).
- Predicted R-RAG5 rate (= LLM picks only 1 source of 2) remains unknown;
  original 50% prediction stands as prior for retest.
