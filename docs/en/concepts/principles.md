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

**Why:** a Phase like `revise` should be drop-in usable from any skill that produces a draft. If Phase carried a "next phase" field, it would couple to a specific workflow and stop being reusable.

## P2. Skill defines structure

A Skill declares `entry_phase`, `graph` (allowed transitions between phases), and `final_output_schema`. Phase connections are **never** defined inside Phase files.

**Why:** structure is a Skill-level concern. Mixing it into Phase would prevent reuse and force every Phase rewrite when the workflow changes.

## P3. OS controls execution

The OS — not the LLM, not the Skill — is the runtime engine. It builds the context frame, calls the LLM, validates outputs, executes Control IR ops, manages transitions, and emits events.

**Why:** keeping orchestration out of the LLM's hands is what makes reyn a *constrained* decision engine. The LLM is a tool the OS uses; the OS is not a tool the LLM uses.

## P4. LLM is a constrained decision engine

The LLM chooses among:

- the next phase, OR `finish`
- an artifact (matching the chosen target's input schema)
- a list of Control IR ops (file reads, ask_user, sub-skill calls, etc.)

It MUST choose only from OS-provided transitions. If the LLM hallucinates a phase name not in the graph, the OS rejects the output.

**Why:** unbounded LLM control flow is unstable. Constraining the choice to a small set of OS-validated options is what makes the system replayable, debuggable, and safe.

## P5. No output schema in Phase

Output schema is determined by:

- the input schema of the next phase, OR
- the skill's `final_output_schema`

**Why:** double-declaration causes drift. Today's "next phase = X" fixes the output to "schema of X.input"; if X changes, the output adapts automatically.

## P6. Skill owns final output

Only Skill defines the final output schema. The OS validates the LLM's final artifact against it.

**Why:** "what does this skill return?" is a Skill-level contract, not a Phase decision. Splitting it across phases would make refactoring brittle.

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

**Why:** if Phase instructions duplicated schema info, schema changes would silently desync. The OS's runtime injection is the single source of truth.

## See also

- [architecture.md](architecture.md) — how the layers fit together
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — responsibility boundaries
- [Reference: llm-output-contract](../reference/runtime/llm-output-contract.md)
- [Agent engineering — seven lenses](agent-engineering/index.md) — reyn read through external engineering perspectives
