---
type: concept
topic: architecture
audience: [human, agent]
---

# Tool Contract Design

How the LLM acts on the world: the typed envelope for side effects, the typed envelope for decisions, and the deterministic hook that runs before the LLM is even called. A clean tool contract is what lets validation, replay, and re-prompt all share the same machinery.

## How reyn handles it

Three contracts, all schema-anchored:

### 1. Control IR — the side-effect envelope

Every side effect (file I/O, asking the user, invoking a sub-skill, running shell, linting) is a JSON object with a `kind` discriminator. The OS dispatches each op against its kind's schema:

```json
{"kind": "file", "op": "read", "path": "src/foo.py"}
{"kind": "ask_user", "question": "Which model?", "suggestions": [...]}
{"kind": "run_skill", "skill": "recall_memory", "input": {...}}
```

Eight op kinds today (`file`, `ask_user`, `run_skill`, `lint`, `shell`, `mcp`, `web_search`, `web_fetch`). Available ops are injected into the LLM's context per phase as `available_control_ops` — phase markdown never describes the syntax (P8).

Each phase narrows that set further with `allowed_ops` in its frontmatter (default `[file, ask_user]`). The OS shows only the listed kinds to the LLM and rejects anything else the LLM emits anyway. This is two-edged: it prevents drift (a `write_memory` extract phase can't accidentally use `web_search` because it sees flash-lite and decides to "look up" a name), and it shrinks the prompt — irrelevant op descriptions are not paid for in tokens.

### 2. Candidate outputs — the decision envelope

For every phase, the OS computes the set of legal next moves: each allowed next phase (or `end`), with the input schema it expects. The LLM picks one and produces a matching artifact:

```json
{
  "control": {"type": "transition", "decision": "continue", "next_phase": "review", ...},
  "artifact": {"type": "draft", "data": {...}},
  "control_ir": [...]
}
```

The shape is fixed; the discriminators are validated; the artifact is checked against the chosen target's schema. Anything off-contract is rejected.

### 3. Preprocessor — deterministic enrichment

A phase may declare a chain that runs **before** the LLM is called: invoke a sub-skill, iterate over a list, validate against a schema, run a Python function. The result lands at a named slot in the LLM's input — phases reference the slot by name and don't need to know it came from a preprocessor.

This is what lets stdlib skills compose without imperative code: `eval` iterates `judge_phase` over per-criterion requests; `skill_router` calls `recall_memory` before deciding which skill to dispatch.

## Why type the contracts so aggressively

Three properties fall out of "everything has a schema":

- **Reject early.** Malformed output triggers a re-prompt before any side effect runs.
- **Replay safely.** A saved event log can be re-rendered without re-invoking the LLM, because every artifact and op was validated at write-time.
- **Compose without surprises.** A sub-skill's output is a typed artifact; the calling phase consumes it like any input.

## Where it's still thin

The five Control IR kinds cover most workflows but more are likely needed as the ecosystem grows. MCP integration exists at the runtime layer (skills can declare MCP servers in permissions and the LLM gets MCP tools as ops); the surface area will grow. Extending the contract is intentionally cheap: add a kind to the OS, declare it in `available_control_ops`, and every skill can use it.

## See also

- [Reference: control-ir](../../reference/runtime/control-ir.md)
- [Reference: llm-output-contract](../../reference/runtime/llm-output-contract.md)
- [Reference: preprocessor](../../reference/dsl/preprocessor.md)
- [Reference: artifact-yaml](../../reference/dsl/artifact-yaml.md)
- [system-design.md](system-design.md) — what the contract makes possible
- [reliability-engineering.md](reliability-engineering.md) — how rejection is handled
