# FP-0026: Op/Permission Cross-Layer Coherence — `reyn skill validate` + auto-derived permission requirements

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Three separate declaration surfaces — phase `allowed_ops`, skill `permissions`, and `reyn.yaml` runtime config — currently have no cross-layer consistency enforcement. Inconsistencies are discovered at runtime rather than at authoring time, creating two distinct failure modes with different discovery timing and high cognitive burden for skill authors. This FP proposes (A) a `reyn skill validate` CLI command that checks cross-layer consistency at authoring time, (B) OS-level consistency warnings at skill load time, and (C) guidance notes in `op_catalog` descriptions for Tier 2-3 ops to close the meta-skill authoring gap.

---

## Motivation

### Three declaration surfaces, no coherence check

Skill authors must write related permission/op information in three separate places:

```
Phase frontmatter   →  allowed_ops: [shell, file]
Skill frontmatter   →  permissions: { shell: { ... } }
reyn.yaml           →  shell: allow
```

The OS validates each layer independently but performs no cross-layer consistency check. A skill that is internally inconsistent (phase uses `shell`, skill lacks `permissions.shell`) passes all individual checks and fails only at first runtime execution of the `shell` op.

### Two failure modes with different discovery timing

**Mode A — phase declaration error** (fast feedback):
LLM outputs an op not in `available_control_ops` → immediately REJECTED at OS validation.
Clear, deterministic, caught before any side effects.

**Mode B — permission declaration error** (slow, surprising):
LLM outputs an op that IS in `allowed_ops` → passes phase validation → reaches execution → `PermissionError` at runtime, potentially after earlier ops in the same `control_ir` batch have already executed.

Mode B is the failure mode that `reyn skill validate` is designed to catch.

### Skill author cognitive burden

`allowed_ops` (phase-level) and `permissions` (skill-level) appear to describe the same thing from two angles. The distinction — one governs LLM output, the other governs OS execution — requires understanding the internal OS architecture. It is not surfaced to skill authors, and no tooling helps bridge the gap.

### Meta-skill op_catalog confusion

Meta-skills (`skill_builder`, `skill_improver`, `skill_importer`) receive the full `op_catalog` to generate phase frontmatter for target skills. A meta-skill LLM may correctly write `allowed_ops: [shell]` for a generated phase — the op exists in the catalog — but if no guidance connects this to the required `permissions.shell` declaration in the target skill, the generated skill will PermissionError at runtime. The current `op_catalog` descriptions carry no such signal.

---

## Proposed implementation

### Component A — `reyn skill validate` CLI (SMALL)

New subcommand under `reyn skill`:

```
reyn skill validate <skill_name>
reyn skill validate --all      # validate all installed skills
```

Validation logic:

```python
def _requires_declaration(op_kind: str) -> bool:
    """True for Tier 2-3 ops that need explicit skill-level permission declaration."""
    return op_kind in {"shell", "mcp", "file_outside_zone"}  # extend as Tier model grows

def validate_skill(skill: Skill) -> list[ValidationIssue]:
    issues = []
    required = {
        op_kind
        for phase in skill.phases.values()
        for op_kind in phase.allowed_ops
        if _requires_declaration(op_kind)
    }
    declared = set(skill.permissions.keys())

    for op_kind in required - declared:
        issues.append(ValidationError(
            code="missing_permission_declaration",
            message=(
                f"A phase uses '{op_kind}' in allowed_ops "
                f"but skill has no permissions.{op_kind} declaration — "
                f"will PermissionError at runtime."
            ),
        ))
    for op_kind in declared - required:
        issues.append(ValidationWarning(
            code="unused_permission_declaration",
            message=(
                f"Skill declares permissions.{op_kind} "
                f"but no phase lists it in allowed_ops."
            ),
        ))
    return issues
```

**Tier 0-1 ops are not checked** — `run_skill`, `ask_user`, `web_fetch`, `web_search` do not require explicit skill-level declaration per the Tier model (FP-0022).

**Integration:**
- `reyn skill install <name>` — runs validation automatically; warnings are non-blocking, errors print prominently
- `reyn skill validate <name>` — explicit check; exits non-zero on errors (suitable as a CI gate for skill repositories)

### Component B — Load-time consistency warning (SMALL)

At skill load (before any LLM call), the OS computes the effective permission requirement set and warns if it exceeds the declared set:

```python
# src/reyn/kernel/skill_loader.py (or equivalent)
effective_required = {
    op_kind
    for phase in skill.phases.values()
    for op_kind in phase.allowed_ops
    if _requires_declaration(op_kind)
}
declared = set(skill.permissions.keys())
missing = effective_required - declared
if missing:
    logger.warning(
        "Skill '%s': phase allowed_ops includes %s but skill has no "
        "permissions declaration for them — will PermissionError at runtime. "
        "Run `reyn skill validate %s` to see details.",
        skill.name, missing, skill.name,
    )
```

This moves Mode B discovery from first execution to skill load — one step earlier, before the user has submitted a request.

### Component C — op_catalog description note for Tier 2-3 ops (SMALL)

In `src/reyn/kernel/control_ir_executor.py`, add a note to the `description` of each Tier 2-3 op spec:

```python
ControlIROpSpec(
    kind="shell",
    description=(
        "Execute a shell command. "
        "Requires skill frontmatter: permissions.shell. "
        "Skills that use this op in allowed_ops must declare permissions.shell "
        "or execution will PermissionError."
    ),
    example=...,
)
```

This closes the meta-skill LLM gap: when a meta-skill reads the `op_catalog` and generates `allowed_ops: [shell]` for a target phase, it also receives the signal to generate `permissions.shell` in the target skill's frontmatter.

---

## Target files

| File | Change |
|---|---|
| `src/reyn/cli/skill.py` | Add `validate` subcommand (Component A) |
| `src/reyn/kernel/skill_loader.py` | Add load-time consistency warning (Component B) |
| `src/reyn/kernel/control_ir_executor.py` | Add `_requires_declaration()` helper; update Tier 2-3 op descriptions (Component C) |
| `docs/deep-dives/contributing/skill-authoring.md` | Document the three layers and `reyn skill validate` |

---

## Dependencies

None. Works with the existing permission and `allowed_ops` infrastructure as-is.
FP-0022 (Tier model) defines which ops fall into Tier 2-3; `_requires_declaration()` should stay in sync with that model.

---

## Cost estimate

| Component | Task | Cost |
|---|---|---|
| A | `reyn skill validate` CLI + validation logic | SMALL |
| B | Load-time consistency warning | SMALL |
| C | op_catalog description notes for Tier 2-3 ops | SMALL |
| **Total** | | **SMALL** |

All three components are additive — no existing behavior changes.

---

## Verification

1. Skill with `allowed_ops: [shell]` but no `permissions.shell` → `reyn skill validate` reports `missing_permission_declaration` and exits non-zero.
2. Skill with `permissions.shell` but no phase uses `shell` → `reyn skill validate` reports `unused_permission_declaration` warning, exits zero.
3. `reyn skill install` on a misconfigured skill → warning in output (install is not blocked).
4. Skill load for a misconfigured skill → warning log with "will PermissionError at runtime" and `reyn skill validate` hint.
5. Meta-skill generating `allowed_ops: [shell]` for a new phase → op_catalog description prompts LLM to also generate `permissions.shell` in target skill frontmatter.
6. Tier 0-1 ops (`web_fetch`, `ask_user`) → no validation issue even if no `permissions` entry.

---

## Related

- FP-0022 (`0022-permission-tier-model.md`) — Tier 0-3 model defining which ops require declaration
- `src/reyn/kernel/runtime.py` — `_build_context_frame()`, where `available_control_ops` is filtered from `allowed_ops`
- `src/reyn/kernel/control_ir_executor.py` — `available_ops()`, where op kinds and descriptions are defined
- `docs/concepts/permission-model.md` — conceptual permission layer documentation
