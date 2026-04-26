# CLAUDE.md

## Agent OS - Concept & Constraints

This document defines the core concepts and constraints of the Agent OS.
You MUST follow these rules when implementing the system.

---

# 1. Core Architecture

The system is composed of the following layers:

```
User → Agent → App → OS → Phase → Workspace
                 ↘ Event (record everything)
```

---

# 2. Core Principles (CRITICAL)

## P1. Phase is Stateless and Reusable

* A Phase ONLY defines `input_schema`
* A Phase DOES NOT know:

  * next phase
  * output schema
  * app structure

## P2. App Defines Structure

* App defines:

  * `entry_phase`
  * `graph` (phase transitions)
  * `final_output_schema`
* Phase connections MUST NOT be defined in Phase

## P3. OS Controls Execution

* OS is the runtime engine
* OS is responsible for:

  * building context
  * calling LLM
  * validating outputs
  * executing Control IR
  * managing transitions
  * emitting Events

## P4. LLM is a Constrained Decision Engine

* LLM chooses:

  * next phase OR finish
  * artifact
  * control_ir
* LLM MUST choose ONLY from OS-provided transitions

## P5. No Output Schema in Phase

* Output is determined by:

  * next phase input schema OR
  * app final_output_schema

## P6. App Owns Final Output

* Only App defines final output schema
* OS validates final output against it

## P7. OS is App-Agnostic (CRITICAL)

* OS MUST NOT contain any phase name, artifact type name, or field name specific to any App
* Detection rule: if a string literal that names a specific phase (`"revise"`, `"draft_article"`, etc.) or a specific field (`"title"`, `"body"`, `"quality_notes"`) appears in OS code, it is a violation
* When a new App is added, the OS code MUST NOT change

Root causes to watch for:

* Fallback logic that fabricates app-specific fields → return raw artifact data instead
* Control decision vocabulary that encodes app concepts (`decision="revise"`) → use only OS-level values: `continue | finish | abort`
* Hardcoded artifact type names in any OS module

## P8. Phase Instructions Contain Only Domain Logic

* Phase instructions MUST NOT enumerate output artifact fields
* Phase instructions MUST NOT describe Control IR format
* These concerns are injected by the OS at runtime via `candidate_outputs` and `available_control_ops`
* Legitimate instruction content: WHAT to analyze/generate/decide, WHEN to use which candidate, domain-specific rules

---

# 3. Core Components

## Agent

* Interprets user intent
* Selects or generates an App
* Does NOT execute phases

## App

* Defines phase graph
* Defines final output schema

## Phase

* Reusable processing unit
* Defines only input schema + instructions

## OS

* Runtime executor
* Owns control flow

## Workspace

* Stores all artifacts and files
* Shared across phases

## Artifact

* Structured data passed between phases

## Event

* Every state change MUST be recorded as an event

---

# 4. Execution Model

## Runtime Loop

1. Build Context Frame
2. Call LLM
3. Receive:

   * next_phase OR finish
   * artifact
   * control_ir
4. Validate output
5. Execute Control IR
6. Update Workspace
7. Emit Event
8. Repeat

---

# 5. Context Frame

The OS MUST construct the context for LLM. ContextFrame is read-only, regenerated every phase, never persisted.

```json
{
  "current_phase": "...",
  "current_phase_role": "...",
  "instructions": "...",
  "input_artifact": {"type": "...", "data": {}},
  "execution": {
    "path": ["phase_a → phase_b"],
    "current_visit": 1,
    "total_steps": 3
  },
  "candidate_outputs": [
    {
      "next_phase": "phase_name or end",
      "control_type": "transition|finish",
      "schema_name": "artifact_type_name",
      "artifact_schema": {},
      "description": "..."
    }
  ],
  "finish_criteria": [],
  "constraints": {"max_phase_visits": null},
  "available_control_ops": [
    {"kind": "file", "description": "...", "example": {}}
  ],
  "output_language": "ja"
}
```

---

# 6. LLM Output Contract

All phases use a single unified format. The OS rejects output that does not conform.

```json
{
  "control": {
    "type": "transition|finish|abort",
    "decision": "continue|finish|abort",
    "next_phase": "<phase_name> or null",
    "confidence": 0.0,
    "reason": {"summary": "one-sentence explanation"}
  },
  "artifact": {"type": "<schema_name>", "data": {}},
  "control_ir": []
}
```

## control.decision values (OS-level only)

* `"continue"` — normal transition to any next phase (including revision loops)
* `"finish"` — workflow ends. Requires `type="finish"` and `next_phase=null`
* `"abort"` — unrecoverable error. Requires `type="abort"` and `next_phase=null`

`"revise"` is NOT a valid decision value. It was removed because it encodes an app-specific concept (P7).
Any transition to a "revise" phase uses `decision="continue"`.

## Consistency rules (violations are rejected)

* `type="finish"` → `decision="finish"`, `next_phase=null`
* `type="transition"` → `next_phase` is non-null
* `type="abort"` → `decision="abort"`, `next_phase=null`

## control_ir

List of side-effect operations. Available kinds are injected via `available_control_ops` in ContextFrame.

* `file` — read or write a file in the workspace
* `ask_user` — pause phase, ask user a question, re-inject response as `user_message` into the same phase

---

# 7. Validation Rules (MANDATORY)

OS MUST validate:

### Transition

* next_phase is allowed by App graph
* artifact matches next_phase.input_schema

### Finish

* finishing is allowed
* final_output matches app.final_output_schema

---

# 8. Workspace Model

* Workspace is the single source of truth for data
* All files, tool outputs, and artifacts live here
* Phases may read/write via Control IR

---

# 9. Event Model

* Every action MUST emit an event

Examples:

* phase_started
* phase_completed
* llm_called
* tool_executed
* artifact_created
* workspace_updated

Event log MUST allow replay in the future

---

# 10. Strict Constraints (DO NOT BREAK)

* NEVER define transitions inside Phase
* NEVER define output schema inside Phase
* NEVER allow LLM to choose arbitrary next phase
* ALWAYS validate LLM output
* ALWAYS emit events
* NEVER put app-specific phase names, artifact type names, or field names in OS code (P7)
* NEVER enumerate output artifact fields in Phase instructions (P8)
* NEVER describe Control IR format in Phase instructions — inject via available_control_ops (P8)

---

# 11. Input Handling

## CLI Input

* JSON string → used as-is (structured artifact)
* Natural language string → wrapped as `{"type": "user_message", "data": {"text": "..."}}`
* The OS does NOT parse or structure natural language input

## user_message Artifact

* Shared artifact defined in `dsl/shared/artifacts/user_message.md`
* Apps that accept natural language input declare `input: user_message | <other_artifact>` in their entry phase
* Structuring the natural language into domain artifacts is the Phase's responsibility

## ask_user (User Intervention)

When a phase needs information it cannot infer:

1. Phase emits `{"kind": "ask_user", "question": "...", "suggestions": [...]}` in `control_ir`
2. OS prints the question and reads user input from stdin
3. OS merges the original input + Q&A into a `user_message` artifact
4. OS re-runs the **same phase** with the merged artifact (visit count does not increment)
5. Events emitted: `user_intervention_requested`, `user_intervention_received`

Responsibility boundary:
* OS — collects input, re-injects, emits events
* Phase — decides WHEN to ask and WHAT to ask (domain logic)

---

# 13. MVP Scope

You are currently implementing MVP.

DO NOT implement:

* multi-agent
* parallel execution
* persistence DB
* UI
* evaluation system

Focus ONLY on:

* phase execution loop
* context construction
* transition validation
* event emission

---

# 14. Goal

The goal of MVP is NOT performance.

The goal is:

> Verify that phase transitions driven by LLM + constrained context are stable and valid
