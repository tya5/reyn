# ADR-0020: Skill-only permissions — Phase.permissions field removed

**Status**: Accepted (2026-05-04)
**Track**: Wave 2 of postprocessor follow-up; supersedes implicit phase-level permission semantics

## Context

Reyn historically declared permissions at the **phase** frontmatter level
(`phase.md` → `permissions:` block). Each phase declared the MCP servers,
shell access, file paths, and python steps it needed; the OS computed a
skill-level union at expand time for the `startup_guard` and postprocessor
hooks.

The postprocessor work (ADR-0017 family) introduced skill-level hooks that
needed a skill-level permission scope. Two commits (246ce42, 7b9adc1)
began migrating stdlib skills to declare `permissions:` at the skill.md
frontmatter and added an explicit-or-fallback path in the expander.

At that point, three placement strategies were on the table:

- **案 1**: Both phase and skill may declare `permissions:`. Skill aggregates
  the union.
- **案 1 改**: Phase declares; skill may override with a narrower or wider
  set. Two-level merge.
- **案 2**: Skill is the only declaration site. Phase-level field deleted.

## Considered alternatives

- **案 1 — dual declaration, union aggregate.** Preserves backward
  compatibility for non-migrated skills. Adds complexity: two sources of
  truth, merge semantics to reason about, audit logic must walk both levels.
  Startup audit is O(phases × fields). Cognitive load: "does this MCP server
  need to appear in phase.md, skill.md, or both?"

- **案 1 改 — phase declares base, skill can override.** Allows per-phase
  granularity (phase A read-only, phase B write) while skill holds the
  aggregate. Implementation is the most complex: override semantics must
  be specified and enforced. Two merge orderings (phase→skill vs skill→phase)
  with different security implications. Hard to audit (O(phases + skill
  overrides)).

- **案 2 — skill only.** Single source of truth. Startup audit is O(1) field
  reads on `Skill.permissions`. Cognitive load minimal: one place to look.
  Loses phase-level granularity — "phase A may only read, phase B may write"
  is not expressible without a future explicit feature. Acceptable for
  current stdlib skills (permissions are skill-wide in practice).

## Decision

**Adopt 案 2.**

- `Phase.permissions` field deleted from `schemas/models.py` (`Phase` BaseModel).
- `PhaseDef.permissions` field deleted from `compiler/ir.py`.
- `parser.parse_phase` hard-rejects any `permissions:` key in phase frontmatter
  with a `ValueError` pointing to `skill.md frontmatter`.
- `expander.expand_skill` uses `PermissionDecl.from_dict(skill_def.permissions)`
  directly; `_union_phase_permissions` function deleted.
- `permissions.py` module docstring and all `require_*` error messages updated
  from "phase permissions" / "phase frontmatter" to "skill permissions" /
  "skill.md frontmatter".

## Consequences

**Positive:**

- Single source of truth for permissions. Audit is O(1): read `skill.permissions`.
- `startup_guard` code simplified — no phase iteration, no union logic.
- Cognitive load reduced: authors declare permissions once, in skill.md.
- Error messages now point to the correct file (`skill.md frontmatter`).
- `_union_phase_permissions` dead code eliminated.

**Negative:**

- Lost phase-level granularity: it is no longer possible to declare "phase A
  may only read file X; phase B may also write it." All phases in a skill
  share the skill-level permission scope.

**Precluded:**

- Phase-level granularity restoration without explicit "案 1 改" feature work
  (new `Phase.permission_override` or similar field with documented override
  semantics). This would be a new ADR, not a reversal of this one.

## References

- Commit 246ce42 — added `Skill.permissions` field (foundation)
- Commit 7b9adc1 — migrated stdlib skills to skill.md `permissions:` declaration
- This commit — final deletion of `Phase.permissions` field (案 2 complete)
- `docs/en/decisions/0006-schema-version-refuse-policy.md` — precedent for
  hard-rejection of removed fields during pre-1.0 (refuse + migration message)
- `docs/en/concepts/principles.md` P5 (workspace as single source of truth)
  — same philosophy applied to permission declaration
