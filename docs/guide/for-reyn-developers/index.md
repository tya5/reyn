---
type: landing
topic: os-development
audience: [human]
---

# For Reyn developers

Orientation for contributors to the Reyn OS core. If you're adding a new op kind, fixing a runtime bug, or extending the event system, start here.

If you're building skills on top of Reyn rather than modifying the OS itself, see [For skill authors](../for-skill-authors/index.md) instead.

---

## Read first

**`CLAUDE.md`** (in the repo root) — the invariants every code-writing agent (and human contributor) must follow. P1–P8 are hard constraints, not guidelines.

**[concepts/architecture/principles.md](../../concepts/architecture/principles.md)** — the *why* behind P1–P8, with worked examples.

**[principles-and-code.md](principles-and-code.md)** — P1–P8 mapped to the exact files and classes that enforce them. Read this when you need to find where something lives.

---

## The OS in one paragraph

```
User → Agent → Skill → OS → Phase → Workspace
```

The OS (`kernel/runtime.py`) is the only thing that calls the LLM, executes Control IR ops, validates outputs, and emits events. Skills describe *what* to do; the OS does *how*. A new skill must never require an OS change (P7).

---

## How-tos

### Adding capabilities

- **[Add a new op kind](add-an-op-kind.md)** — register a new Control IR operation. Three touch points: model, registry, handler.
- **[Write LLMReplay tests](write-replay-tests.md)** — test LLM-dependent behaviour deterministically without live API calls.

### Understanding the system

- **[P1–P8 and the code that enforces them](principles-and-code.md)** — file-by-file map of how each principle is mechanically upheld.

---

## Key source files

| File | What it does |
|---|---|
| `src/reyn/kernel/runtime.py` | Main OS loop — LLM call, validation, op execution, events |
| `src/reyn/kernel/control_ir_executor.py` | Dispatches Control IR ops from the OS to op handlers |
| `src/reyn/op_runtime/registry.py` | Single source of truth for op kinds, Pydantic models, purity classification |
| `src/reyn/context_builder.py` | Builds the ContextFrame injected into every LLM call (P4 candidates here) |
| `src/reyn/schemas/models.py` | All Pydantic models — Phase, Skill, SkillGraph, ControlIROp, CandidateOutput |
| `src/reyn/events/events.py` | Append-only EventLog (P6) |
| `src/reyn/workspace/workspace.py` | Workspace read/write with permission gating (P5) |
| `src/reyn/compiler/linter.py` | Static validation — graph cycles, allowed_ops spelling, P7 checks |
| `src/reyn/compiler/expander.py` | Loads and expands Skill + Phase from `.md` + `.yaml` files |

---

## Testing policy

Read **[deep-dives/contributing/testing.md](../../deep-dives/contributing/testing.md)** before writing any test. Key rules:

- Tests belong to exactly one Tier (1: Contract / 2: OS invariant / 3: LLM-replay).
- Never use `MagicMock` / `AsyncMock` / `patch` on collaborators. Use real instances or `LLMReplay`.
- Never assert on private state. Use public surface or `snapshot()`.
- Tier 4 ("doesn't fit a tier") → don't write it.

The full rationale is in the testing doc — the rules are non-obvious and violation is easy.

---

## See also

- [Reference: Control IR](../../reference/runtime/control-ir.md) — op kind catalogue (must stay in sync with `OP_KIND_MODEL_MAP`)
- [Reference: LLM output contract](../../reference/runtime/llm-output-contract.md) — the JSON the OS validates on every LLM call
- [Reference: Events](../../reference/runtime/events.md) — event kind list and JSONL schema
- [ADR index](../../deep-dives/decisions/README.md) — architectural decisions and the rejected alternatives
