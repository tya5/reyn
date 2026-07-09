---
type: concept
topic: architecture
audience: [human, agent]
---

# Tool Contract Design

> **Status: partially stale.** This page was written against the phase-graph skill
> engine, deleted in a later engine-deletion arc. The "Candidate outputs" and
> "Preprocessor" sections described that engine specifically (`next_phase` transitions,
> `skill_router`/preprocessor chains) and have been removed — confirmed via direct grep
> that neither concept exists in current source. The "Control IR" section below is kept
> and corrected: the side-effect envelope itself is still live, just under
> `schemas/models.py` (not `op_runtime/registry.py` — `OP_KIND_MODEL_MAP` relocated
> there per CLAUDE.md's OP_KIND_MODEL_MAP/control-ir.md sync rule) and with a current op-kind list.

How the LLM acts on the world: the typed envelope for side effects. A clean tool contract is what lets validation and replay share the same machinery.

## How reyn handles it

### Control IR — the side-effect envelope

Every side effect (file I/O, asking the user, presenting data, running a sandboxed command, calling an MCP tool) is a JSON object with a `kind` discriminator. The OS dispatches each op against its kind's schema:

```json
{"kind": "read_file", "path": "src/foo.py"}
{"kind": "ask_user", "question": "Which model?", "suggestions": [...]}
{"kind": "mcp", "server": "github", "tool": "create_issue", "args": {...}}
```

The op kinds live in `OP_KIND_MODEL_MAP` (`schemas/models.py`): the
fine-grained file ops (`read_file`, `write_file`, `edit_file`, `delete_file`,
`glob_files`, `grep_files`), plus `ask_user`, `present`, `sandboxed_exec`,
`mcp` (and its resource/prompt/subscribe variants), `web_search`, `web_fetch`,
and the RAG / task / compaction kinds. See [Control IR](../../reference/runtime/control-ir.md)
for the full, current catalog.

## Why type the contracts so aggressively

Two properties fall out of "every op has a schema":

- **Reject early.** Malformed output triggers a validation error before any side effect runs.
- **Replay safely.** A saved event log can be re-rendered without re-invoking the LLM, because every op was validated at write-time.

## See also

- [Reference: control-ir](../../reference/runtime/control-ir.md)
- [system-design.md](system-design.md) — what the contract makes possible
- [reliability-engineering.md](reliability-engineering.md) — how rejection is handled
