---
type: phase
name: analyze_execution
input: execution_summary
role: quality_analyst
---

Deeply analyze the target app's execution to identify quality issues and improvement opportunities.

Step 1 — Read events JSONL:
  Glob for the events file using events_glob. Read it line by line (it is JSONL).
  Focus on these event types:
  - phase_started / phase_completed: count visits, retries, confidence scores
  - phase_retry: indicates a validation failure — note the error message
  - llm_response_received: check if the LLM output needed normalization or produced low confidence
  - workflow_aborted: serious failure — note reason
  - artifact_created: note which artifacts were produced and their keys

Step 2 — Read artifact files:
  Glob for artifact JSON files using artifacts_glob. Read each one.
  Evaluate: are the artifact fields populated with meaningful content?
  Are required fields present? Is the content depth appropriate for the task?

Step 3 — Read target app DSL files to understand the design:
  Read app.md, and each phase .md and artifact .md under app_dsl_path's parent directory.
  Use glob: e.g. "reyn/project/{app_name}/**/*.md"

Step 4 — Synthesize findings:
For each phase:
  - How many visits (retries) did it take?
  - Were there any validation errors or normalizations?
  - Is the produced artifact content high quality for the phase's goal?
  - Are the phase instructions clear and specific enough?

Identify concrete issues (not vague observations):
  BAD: "instructions could be better"
  GOOD: "analyze_code phase instructions do not specify what output format to use for component relationships, causing the LLM to omit structured data"

Score overall quality 0–10. Be honest and critical — a score of 8+ means the app needs no major changes.
