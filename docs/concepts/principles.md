---
type: concept
topic: architecture
audience: [human, agent]
---

# Design principles (P1–P8)

reyn's architecture is anchored by eight principles. They're listed in `CLAUDE.md` as constraints for code-writing agents; this page is for humans (and the curious agent) who want to understand **why** they exist.

## P1. Phase is stateless and reusable

A Phase declares only its `input` artifact type and instructions. It does NOT know:

- which phase comes next
- what its output schema looks like
- which skill it belongs to

Output shape is determined externally — by the next phase's `input` schema (on a transition) or by the skill's `final_output` (on a finish). The Phase itself never declares or sees the output schema.

**Why:** a Phase like `revise` should be drop-in usable from any skill that produces a draft. If Phase carried a "next phase" field or duplicated an output schema, it would couple to a specific workflow and stop being reusable. Double-declaration also causes drift: when the next phase changes its input, any Phase that had hardcoded the output shape would silently go stale. Making output shape an external concern eliminates that class of bug entirely.

**Common traps:**

- Frontmatter field `output:` or `output_schema:` → remove it; output is derived from the transition target.
- Instructions that say "you must produce a JSON object with fields `title`, `body`, …" → those fields belong in the artifact schema, not the instructions.

## P2. Skill defines structure and owns final output

A Skill declares `entry`, `graph` (allowed transitions between phases), and `final_output`. Phase connections are **never** defined inside Phase files. The OS validates the LLM's final artifact against `final_output` at finish time — this is the Skill's contract with the outside world.

**Why:** structure is a Skill-level concern. Mixing it into Phase would prevent reuse and force every Phase rewrite when the workflow changes. Similarly, "what does this skill return?" is a Skill-level contract, not a Phase decision. If final output were spread across phases, refactoring the graph would risk silently changing the return type seen by callers.

**Common traps:**

- `final_output` missing from `skill.md` → the OS has nothing to validate finish against.
- Skill graph missing an edge → the LLM offers a transition the OS will reject.
- A phase declaring `can_finish: true` without a `final_output` declared in the skill → the OS will reject the finish.

## P3. OS controls execution

The OS — not the LLM, not the Skill — is the runtime engine. It builds the context frame, calls the LLM, validates outputs, executes Control IR ops, manages transitions, and emits events.

**Why:** keeping orchestration out of the LLM's hands is what makes reyn a *constrained* decision engine. The LLM is a tool the OS uses; the OS is not a tool the LLM uses. If the LLM were allowed to run arbitrary code or choose arbitrary transitions, the system would lose the auditability and predictability that are reyn's core value.

## P4. LLM is a constrained decision engine

The LLM chooses among:

- the next phase, OR `finish`
- an artifact (matching the chosen target's input schema)
- a list of Control IR ops (file reads, ask_user, sub-skill calls, etc.)

It MUST choose only from OS-provided transitions. If the LLM hallucinates a phase name not in the graph, the OS rejects the output.

**Why:** unbounded LLM control flow is unstable. Constraining the choice to a small set of OS-validated options is what makes the system replayable, debuggable, and safe. The LLM's creative latitude is preserved inside phase instructions; its structural latitude is deliberately bounded.

## P5. Workspace is the single source of truth

All data, artifacts, and files passed between phases live in the workspace. Phases read and write only through Control IR (gated by the permission system). In-memory state inside a phase is not trustworthy until it lands in the workspace.

**Why:**

- **Replayability.** Because every write goes through the OS and emits an event ([P6](#p6-events-are-the-audit-truth)), the event log alone is enough to reconstruct what the workflow saw. There is no "hidden state" the OS could be missing.
- **Permission enforcement.** The permission system gates every read and write through Control IR. A phase that bypassed the workspace via in-memory side-channels would evade permission checks entirely.
- **Crash recovery.** If the OS restarts mid-run, the workspace is what survived. Anything that was never written there is lost.

**Common traps:**

- Passing data between phases via a Python preprocessor's return value without writing it to the workspace first → violates P5; use `file.write` + `file.read` or the artifact channel.
- Accumulating state in a module-level variable across LLM calls → invisible to the event log, unrecoverable on crash.

## P6. Events are the audit truth

Every state change emits an event. The event log (`events/`) is append-only and replay-capable. State recovery, debugging, cross-agent tracing, and future hash chaining all derive from events. Anything that mutates state without an event is invisible to the OS.

**Why:**

- **Debuggability.** When something goes wrong, the event log is the first — and usually only — tool needed. Every LLM call, every Control IR op, every validation failure leaves a record.
- **Replay.** A complete event log is a complete description of execution. `reyn events <log>` re-renders a run without re-invoking the LLM.
- **Audit trail.** For environments with compliance requirements, the append-only log is the foundation of an auditable record. Future work may add hash chaining to make tampering detectable.
- **Cross-agent tracing.** When agent A delegates to agent B (which may delegate further), every hop emits events carrying the same `chain_id` minted at the original user submission. Reconstructing a multi-hop chain end-to-end is `grep <chain_id>` across each agent's `events.jsonl`.

**Common traps:**

- Mutating workspace state directly (e.g., writing a file from a preprocessor without the OS knowing) → the OS emits no `write_file` event, so the mutation is invisible to audit and replay.
- Emitting free-form application logs instead of structured events → not replay-capable, not filterable, not part of the audit chain.

## P7. OS is skill-agnostic (CRITICAL)

OS code MUST NOT contain phase names, artifact type names, or field names specific to any Skill.

**Detection rule:** if a string literal that names a specific phase (`"revise"`, `"draft_article"`) or a specific field (`"title"`, `"body"`, `"quality_notes"`) appears in OS code, it is a violation.

**Why:** when a new Skill is added, OS code MUST NOT change. This is what makes reyn extensible — skills come and go, but the runtime is constant.

Common pitfalls to avoid:

- Fallback logic that fabricates skill-specific fields → return raw artifact data instead.
- Decision vocabulary that encodes skill concepts (`decision="revise"`) → use only OS-level values: `continue | finish | abort`.
- Hardcoded artifact type names in any OS module.

## P8. Phase instructions contain only domain logic

Phase instructions MUST NOT enumerate output artifact fields, and MUST NOT describe Control IR format. Both are injected by the OS at runtime via `candidate_outputs` and `available_control_ops`.

**Legitimate instruction content:**

- WHAT to analyze, generate, or decide
- WHEN to use which candidate transition
- Domain-specific rules

**Why:** if Phase instructions duplicated schema info, schema changes would silently desync. The OS's runtime injection is the single source of truth for what the LLM sees about output shape and available ops. Instructions that re-state schema info also bloat the context window and invite the LLM to produce fields the current target doesn't expect.

## See also

- [architecture.md](architecture.md) — how the layers fit together
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — responsibility boundaries
- [workspace.md](workspace.md) — Workspace in depth (P5)
- [events.md](events.md) — Events in depth (P6)
- [Reference: llm-output-contract](../reference/runtime/llm-output-contract.md)
- [Agent engineering — seven lenses](agent-engineering/index.md) — reyn read through external engineering perspectives
