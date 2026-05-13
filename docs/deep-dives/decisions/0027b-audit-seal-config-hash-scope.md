# ADR-0027b: config_hash scope for AuditSeal

**Status**: Proposed
**Date**: 2026-05-13
**Depends on**: ADR-0027 (AuditSeal Separation)

---

## Context

ADR-0027 defines `AuditContext` (written at skill-run start) with a
`config_hash` field:

```json
{
  "run_id": "abc123",
  "config_hash": "sha256:..."
}
```

The parent ADR deferred the question of **what exactly is hashed** to
produce `config_hash`. This matters because:

1. The hash is used by a verifier to detect configuration drift between
   the time of execution and the time of audit.
2. The scope of the hash determines which changes are **detectable**
   (included in the hash) vs. **invisible** (excluded).
3. Overly broad scope produces excessive seal invalidation when
   inconsequential configuration changes occur (noise). Overly narrow
   scope misses changes that materially affect LLM behavior (blind spots).

The competitor Hermes #487 is pursuing deterministic reproducibility —
a stricter goal that requires pinning model weights, tool definitions, and
exact prompt text. Reyn's design choice here signals whether `config_hash`
is oriented toward **compliance auditability** (was the system configured
as declared?) or **deterministic reproducibility** (could this exact run
be reproduced byte-for-byte?).

---

## Decision drivers

- **Enterprise compliance demand**: regulated environments (SOX, HIPAA)
  primarily ask "was the agent configured as the policy document stated?"
  — a configuration integrity question, not a reproducibility question.
- **OSS light user discipline**: light users want audit without operational
  overhead; frequent seal invalidation due to inconsequential config changes
  would erode trust in the seal system.
- **Hermes #487 positioning**: if Reyn pursues full deterministic
  reproducibility, the scope must include model settings (provider, model
  version, temperature). If Reyn scopes to compliance-only, model settings
  may be logged separately in AuditContext metadata without being part of
  the hash.
- **Hash stability**: reyn.yaml structural changes that don't affect skill
  behavior (comments, whitespace, unrelated sections) should not invalidate
  seals.
- **Skill-level granularity**: a skill-specific config change should ideally
  be detectable without re-hashing the entire reyn.yaml.

---

## Options considered

### Option A: Full reyn.yaml hash

Hash the entire reyn.yaml file (canonical form, whitespace-normalized).

**Pros:**
- Simple to implement: one file, one hash.
- Captures all configuration changes, including ones not anticipated at
  design time.

**Cons:**
- Any change to any section of reyn.yaml — including unrelated sections like
  `logging.level` or `retention.events_days` — invalidates the seal and
  triggers a verifier mismatch. This is noisy in practice.
- Comments and formatting changes (whitespace normalization mitigates this
  partially) could still cause drift.
- Multiple skills running concurrently with different effective configs
  (e.g., one skill overrides model) would produce the same `config_hash`
  despite different effective configurations.

### Option B: Skill definition hash only

Hash only the skill definition files relevant to the run: `skill.md` (and
any referenced phase files) for the skill being executed.

**Pros:**
- Directly answers "was this skill's definition modified between runs?"
- Model config changes (provider switches, temperature tuning) are excluded —
  treating them as outside the "skill integrity" concern.
- Skill-granular: each skill run's hash is specific to that skill.
- Stable against unrelated reyn.yaml changes.

**Cons:**
- Does not detect model provider switches or version changes that may
  materially affect LLM output even with the same skill definition.
- Does not detect changes to OS-level configuration that affects skill
  execution (e.g., `audit.seal_unit`, permission defaults).

### Option C: Model settings hash only

Hash the effective model configuration for the run: provider, model name,
model version (if available), and inference parameters (temperature, etc.).

**Pros:**
- Directly answers "was the same LLM used as declared?"
- Aligns with Hermes #487's deterministic reproducibility goal if that
  becomes a design target.

**Cons:**
- Does not detect skill definition changes.
- Model version strings may not be stable (provider-side aliases like
  "gemini-2.5-flash-latest" resolve to different versions over time).
- Creates a false impression of "same config" when only the skill definition
  changed but the model stayed the same.

### Option D: Tiered — multiple hash fields in AuditContext and AuditSeal

The seal carries multiple independent hash fields:

```json
{
  "config_hash": {
    "skill_def": "sha256:...",
    "model_cfg": "sha256:...",
    "os_cfg": "sha256:..."
  }
}
```

A verifier can check any or all of these independently, depending on the
compliance requirement being evaluated.

**Pros:**
- Maximum flexibility: different compliance requirements check different
  sub-hashes.
- No noise from unrelated changes: `skill_def` hash is stable when only
  model config changes.
- Aligns with both the compliance-auditability and the reproducibility goals
  simultaneously.
- Forward compatible: new hash fields can be added without breaking existing
  verifiers (they ignore unknown fields).

**Cons:**
- Higher implementation complexity: three hash inputs must be defined,
  computed, and maintained.
- The `os_cfg` scope needs its own sub-decision (which parts of reyn.yaml
  are "OS config" vs. "skill config" vs. "model config"?).
- Verifier UI must communicate which sub-hash mismatched — requires richer
  error reporting.

---

## Recommendation (proposed direction)

**Option D (tiered)** is the recommended direction.

Rationale:
- Option A is too noisy for practical use.
- Option B and C each capture only half of the relevant concern.
- Option D is the natural composition of B and C. The `os_cfg` scope can
  start narrow (only `audit.*` and `skill.permissions.*` sections of
  reyn.yaml) and expand in later releases.

**Minimal initial scope for Option D:**

| Sub-hash field | Content hashed | Implementation note |
|---|---|---|
| `skill_def` | Canonical content of the skill's `skill.md` + all referenced phase files | Walk skill resolution order; concatenate |
| `model_cfg` | Effective provider + model name + key inference params (temperature, max_tokens) | Derive from resolved reyn.yaml `models.*` section |
| `os_cfg` | `audit.*` section of reyn.yaml (seal policy parameters) | Narrow initial scope; expand on demand |

**Decision at implementation time**: if the tiered approach proves too
complex for the initial AuditSeal implementation, fall back to Option B
(skill definition hash only) with a design note that `model_cfg` hash
will be added in a follow-up. This is the minimum viable compliance-useful
hash.

This recommendation should be re-evaluated at implementation start.

---

## Open questions

1. For `skill_def` hash: should the hash include only `skill.md` and phase
   files, or also the stdlib skill sources that are resolved at runtime
   (e.g., `@sub_skill` references)?
2. For `model_cfg` hash: how should provider-managed model aliases
   (e.g., "gemini-2.5-flash-latest") be handled — hash the alias string,
   or resolve to a pinned version at run time?
3. What is the canonical serialization of reyn.yaml's `audit.*` section for
   the `os_cfg` hash? (JSON-sorted? YAML? Pydantic model dict?)
4. Should `config_hash` in `AuditContext` be a flat string (Option A/B/C)
   or a structured object (Option D)? The schema version must be included
   to allow future evolution.

---

## Related

- ADR-0027: AuditSeal Separation (parent ADR)
- ADR-0027a: hash chain topology
- ADR-0027c: seal_unit and plan-mode integration
- ADR-0027d: writer failure semantics
- Hermes #487 competitive context: `docs/deep-dives/research/competitive/hermes-agent.md`
