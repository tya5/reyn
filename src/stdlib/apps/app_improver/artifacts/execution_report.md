---
type: artifact
name: execution_report
---

# Analysis of the target app's execution: quality, issues, and improvement areas.

app_dsl_path: string

total_phase_steps: integer
  # Total number of LLM calls across all phases.

retry_count: integer
  # Number of phase retries due to validation failures.

aborted: boolean
  # True if the workflow ended with an abort event.

phase_reports:
  type: array
  items:
    type: object
    properties:
      phase:
        type: string
      visit_count:
        type: integer
      issues:
        type: array
        items:
          type: string
      assessment:
        type: string
        # "good" | "acceptable" | "needs_improvement"
    required: [phase, visit_count, issues, assessment]

artifact_reports:
  type: array
  items:
    type: object
    properties:
      path:
        type: string
      artifact_type:
        type: string
      quality:
        type: string
        # "good" | "acceptable" | "needs_improvement"
      notes:
        type: string
    required: [path, artifact_type, quality, notes]

issues:
  type: array
  items:
    type: string
  # Concrete list of problems found across phases and artifacts.

strengths:
  type: array
  items:
    type: string
  # What is working well in the current app design.

quality_score: number
  # 0–10 overall quality score.

improvement_areas: string[]
  # High-level areas that most need improvement.
