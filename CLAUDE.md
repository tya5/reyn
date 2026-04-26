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

The OS MUST construct the context for LLM:

```json
{
  "current_phase": "...",
  "input_artifact": {},
  "history_summary": "...",
  "available_transitions": [
    {
      "phase": "...",
      "input_schema": {}
    }
  ],
  "can_finish": true,
  "final_output_schema": {}
}
```

---

# 6. LLM Output Contract

## Transition

```json
{
  "status": "transition",
  "next_phase": "...",
  "artifact": {},
  "control_ir": []
}
```

## Finish

```json
{
  "status": "finish",
  "final_output": {},
  "control_ir": []
}
```

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

---

# 11. MVP Scope

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

# 12. Goal

The goal of MVP is NOT performance.

The goal is:

> Verify that phase transitions driven by LLM + constrained context are stable and valid
