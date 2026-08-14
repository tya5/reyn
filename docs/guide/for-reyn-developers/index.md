---
type: landing
topic: os-development
audience: [human]
---

# For Reyn developers

Orientation for contributors to the Reyn OS core. If you're adding a new op kind, fixing a runtime bug, or extending the event system, start here.

If you're building on top of Reyn rather than modifying the OS itself, see
[Skills](../../concepts/tools-integrations/skills.md) (writing a `SKILL.md`)
or [Pipeline DSL](../../reference/runtime/pipeline-dsl.md) (writing a
pipeline) instead — there is no single "workflow authoring docs" landing
page today; this line named one that doesn't exist.

---

## Read first

**`CLAUDE.md`** (in the repo root) — the invariants every code-writing agent (and human contributor) must follow. P1–P8 are hard constraints, not guidelines.

**CLAUDE.md** — the *why* behind P1–P8, with worked examples.

**[principles-and-code.md](principles-and-code.md)** — P5–P7 mapped to the exact files and classes that enforce them (P1–P4/P8 were retired with the phase-graph engine, #2434 — see the page's own banner).

---

## The OS in one paragraph

```
User → Agent → Workflow → OS → Phase → Workspace
```

The OS (`kernel/runtime.py`) is the only thing that calls the LLM, executes Control IR ops, validates outputs, and emits events. Workflows describe *what* to do; the OS does *how*. A new workflow must never require an OS change (P7).

---

## How-tos

### Adding capabilities

- **[Add a new op kind](add-an-op-kind.md)** — register a new Control IR operation. Three touch points: model, registry, handler.
- **[Write LLMReplay tests](write-replay-tests.md)** — test LLM-dependent behaviour deterministically without live API calls.

### Benchmarking

- **[Run SWE-bench](run-swe-bench.md)** — run Reyn against SWE-bench: solve a single instance, run a batch, and the optional-dep / honest-skip scoring gotcha.

### Understanding the system

- **[P1–P8 and the code that enforces them](principles-and-code.md)** — P5–P7's file-by-file map is current; P1–P4/P8 (written against the deleted phase-graph skill engine) were retired in place rather than kept as stale mechanism prose (#4705). Read CLAUDE.md's eight lenses for the current framework the retired principles don't 1:1 map onto.

---

## Key source files

| File | What it does |
|---|---|
| `src/reyn/schemas/models.py` | Single source of truth for op kinds — `OP_KIND_MODEL_MAP` (op kind → IROp model) and the `Op` union derived from it |
| `src/reyn/core/op_runtime/registry.py` | Op-handler registration + the `ALL_OP_KINDS` / tool-name view over the map above |
| `src/reyn/core/context_builder.py` | The shared, model-independent per-result inline read cap (`control_ir_inline_cap`, bytes, config-driven — #4381 PR-5) |
| `src/reyn/core/events/events.py` | Append-only EventLog (P6) |
| `src/reyn/data/workspace/workspace.py` | Workspace read/write with permission gating (P5) |

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
- [Reference: Events](../../reference/runtime/events.md) — event kind list and JSONL schema
- [ADR index](../../deep-dives/decisions/README.md) — architectural decisions and the rejected alternatives
