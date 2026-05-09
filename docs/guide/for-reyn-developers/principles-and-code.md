---
type: reference
topic: os-development
audience: [human]
---

# P1–P8 and the code that enforces them

Each of Reyn's eight OS invariants is enforced by a specific combination of type constraints, compiler checks, and runtime validation — not just by convention. This page maps each principle to the exact files and mechanisms that uphold it.

For the *why* behind each principle, see [concepts/principles.md](../../concepts/principles.md).

---

## Quick reference

| Principle | Enforced by | Primary file(s) |
|---|---|---|
| P1 — Phase doesn't know next | Phase model has no `next_phase` field | `schemas/models.py` |
| P2 — Skill owns the graph | `SkillGraph` in Skill; compiler validates DAG | `schemas/models.py`, `compiler/linter.py` |
| P3 — OS executes | `OSRuntime` is the only caller of LLM and Control IR | `kernel/runtime.py` |
| P4 — LLM picks from candidates | `_build_candidates()` gates choices; normalizer rejects unknown | `context_builder.py`, `kernel/runtime.py` |
| P5 — Workspace is SSoT | All writes go through `Workspace` with permission gate | `workspace/workspace.py`, `op_runtime/file.py` |
| P6 — Events are audit truth | `EventLog` is append-only; state recovery reads events | `events/events.py`, `events/state_log.py` |
| P7 — OS is skill-agnostic | `OP_KIND_MODEL_MAP` is the only op catalogue; linter rejects skill-specific strings | `op_runtime/registry.py`, `compiler/linter.py` |
| P8 — Instructions don't list fields | Schema is injected via `candidate_outputs`, not baked into instructions | `context_builder.py`, `kernel/runtime.py` |

---

## P1 — Phase declares only input_schema and instructions

**What it means**: A Phase must not know which phase comes next, what the output schema is, or who its parent Skill is.

**How it's enforced**:

`schemas/models.py` — the `Phase` Pydantic model has no `next_phase` or `output_schema` field. There is nowhere to put it. The expander (`compiler/expander.py`) parses phase frontmatter and would raise a validation error if these fields appeared.

The next phase is determined at runtime by `OSRuntime` from `skill.graph.transitions`, not from anything the Phase knows.

```python
# schemas/models.py
class Phase(BaseModel):
    name: str
    instructions: str
    input_schema: str | None = None
    allowed_ops: list[str] = []
    permissions: PermissionDecl = ...
    # ← no next_phase, no output_schema
```

---

## P2 — Skill declares graph and final_output_schema

**What it means**: Phase connections (who can transition to whom) live in the Skill, not in any Phase.

**How it's enforced**:

`schemas/models.py` — `SkillGraph` holds `transitions: dict[str, list[str]]` and `can_finish_phases: list[str]`.

`compiler/linter.py` — `_find_cycle()` performs a DFS on the transition graph and raises `LintError` if a cycle exists. It also checks that `entry_phase` is reachable in the graph.

`kernel/runtime.py` — at transition time, `OSRuntime` checks `next_phase in skill.graph.transitions[current_phase]` before accepting the LLM's choice.

---

## P3 — OS is the runtime engine

**What it means**: The LLM describes what to do; the OS does it. Skills and phases never call the LLM or execute ops directly.

**How it's enforced**:

`kernel/runtime.py` — `OSRuntime` contains the only call to `call_llm()` (from `llm/llm.py`). No skill code path reaches `call_llm` directly.

`kernel/control_ir_executor.py` — the only place that calls op handlers. Phase instructions can ask for a `file` op; they cannot *execute* one.

The separation is structural: Phases are Pydantic data objects with no methods that do IO. There is no `phase.run()` method.

---

## P4 — LLM picks only from OS-provided candidates

**What it means**: The LLM cannot choose an arbitrary next phase or artifact type. The OS provides an explicit list; the LLM picks from it.

**How it's enforced**:

`context_builder.py` — `build_frame()` calls `_build_candidates()` (in `kernel/runtime.py`) and embeds the result in `ContextFrame.candidate_outputs`. Each candidate carries `next_phase`, `control_type`, `schema_name`, and the full `artifact_schema`.

`kernel/runtime.py` — `_normalizer.normalize()` checks that `control.next_phase` is one of the candidates. Unknown values are rejected before the output is acted on.

```python
# kernel/runtime.py (simplified)
candidates = self._build_candidates(skill, current_phase)
frame = build_frame(..., candidate_outputs=candidates)
raw = await call_llm(frame)
output = normalizer.normalize(raw, allowed_candidates=candidates)
# ↑ raises if output.control.next_phase not in [c.next_phase for c in candidates]
```

---

## P5 — Workspace is the single source of truth

**What it means**: All data passed between phases lives in the workspace. Phases read and write only through Control IR ops, which are gated by the permission system.

**How it's enforced**:

`workspace/workspace.py` — all reads and writes go through `Workspace.read_artifact()` / `write_artifact()` / `write_file()`. There is no in-memory dict that phases share.

`op_runtime/file.py` — the `file` op handler calls `Workspace.write_file()`, which calls `PermissionResolver.check()` before touching the filesystem. Writes that aren't declared in `permissions.file_write` are rejected.

`events/events.py` — `Workspace.write_artifact()` emits a `workspace_updated` event. Any write that doesn't go through Workspace is invisible to the event log and therefore to crash recovery.

---

## P6 — Events are the audit truth

**What it means**: Every state change emits an event. The event log is append-only and replay-capable.

**How it's enforced**:

`events/events.py` — `EventLog` exposes only `emit()` (appends) and `to_list()` / `to_json()` (reads). There is no `delete()`, `update()`, or `truncate()` method.

`events/state_log.py` — the WAL is a JSONL file written with monotonically increasing `seq` values. The recovery path in `kernel/runtime.py` reads this file forward to reconstruct state — it never writes backwards.

`kernel/runtime.py` — every meaningful action (phase start, LLM call, op start, op complete, transition, finish, crash) has a corresponding `emit()` call. Missing an emit is a P6 violation detectable by audit.

---

## P7 — OS code contains no skill-specific strings

**What it means**: No phase name, artifact type, or domain-specific field name appears as a literal in OS code.

**How it's enforced**:

`op_runtime/registry.py` — `OP_KIND_MODEL_MAP` maps op kind strings (e.g. `"file"`, `"mcp"`) to Pydantic models. This is the *only* place op kind strings appear in OS code. A new op kind requires adding one entry here, not scattering the string across modules.

`compiler/linter.py` — `ALL_OP_KINDS = frozenset(OP_KIND_MODEL_MAP.keys())`. The linter uses this set to validate `allowed_ops` in phase frontmatter. A misspelled op kind is a lint error, not a silent runtime failure.

`kernel/control_ir_executor.py` — `_build_phase_tool_catalog()` derives the tool schema for the LLM *from the Pydantic model* (`OP_KIND_MODEL_MAP[kind]`), not from any hardcoded field list.

**Detection rule (from CLAUDE.md)**: if a literal naming a specific phase, artifact type, or field appears in OS code — it's a P7 violation.

---

## P8 — Phase instructions don't enumerate artifact fields

**What it means**: The output artifact schema (its fields and types) is injected at runtime via `candidate_outputs`, not written into `phase.instructions`.

**How it's enforced**:

`context_builder.py` — `build_frame()` takes `candidate_outputs: list[CandidateOutput]`. Each `CandidateOutput` carries `artifact_schema: dict` — the full JSON Schema for the expected output. This is appended to the system prompt by `llm.py` at call time.

`kernel/runtime.py` — `_build_candidates()` reads the schema from `next_phase.input_schema` (for transitions) or `skill.final_output_schema` (for finish). The Phase itself never touches these schemas.

The practical consequence: you can change an artifact's schema without editing any phase instructions. The LLM sees the new schema through `candidate_outputs` on the next run.

---

## Adding a new op kind (3 touch points)

This is the canonical example of P7 in practice: a new op kind requires exactly three changes, all in OS code, none in any skill.

### 1. Define the Pydantic model (`schemas/models.py`)

```python
class MyOpIROp(BaseModel):
    kind: Literal["my_op"]
    target: str
    options: dict = {}

# Add to the ControlIROp union:
ControlIROp = Annotated[
    FileIROp | MCPIROp | ... | MyOpIROp,
    Field(discriminator="kind")
]
```

### 2. Register in the op registry (`op_runtime/registry.py`)

```python
from reyn.schemas.models import MyOpIROp

OP_KIND_MODEL_MAP: dict[str, type[BaseModel]] = {
    ...
    "my_op": MyOpIROp,
}

OP_PURITY: dict[str, OpPurity] = {
    ...
    "my_op": OpPurity.side_effect,  # or pure / world / external
}
```

`ALL_OP_KINDS` updates automatically (it's derived from the map).

### 3. Implement the handler (`op_runtime/my_op.py`)

```python
from reyn.schemas.models import MyOpIROp
from . import register
from .context import OpContext

async def handle(op: MyOpIROp, ctx: OpContext, caller: str) -> dict:
    # execute the op
    return {"kind": "my_op", "status": "ok", ...}

register("my_op", handle)
```

Import it in `op_runtime/__init__.py`:

```python
from . import my_op as _my_op  # noqa: F401, E402
```

**What you get for free** after these 3 steps:
- Linter validates `allowed_ops: [my_op]` in phase frontmatter
- `ControlIRExecutor` dispatches to your handler
- LLM sees the op's schema in the system prompt (via `_build_phase_tool_catalog`)
- Purity classification drives correct memo/replay behaviour on resume

### Also update the reference doc

`docs/reference/runtime/control-ir.md` must stay in sync with `OP_KIND_MODEL_MAP` (from CLAUDE.md). Add a section for `my_op` in the same PR that adds the implementation.

---

## See also

- [concepts/principles.md](../../concepts/principles.md) — full rationale for each principle
- [reference/runtime/control-ir.md](../../reference/runtime/control-ir.md) — op kind catalogue
- [reference/runtime/llm-output-contract.md](../../reference/runtime/llm-output-contract.md) — the JSON the OS validates
- [deep-dives/contributing/testing.md](../../deep-dives/contributing/testing.md) — test tier requirements
