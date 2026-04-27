---
type: app
name: app_improver
entry: prepare
final_output: improvement_result
final_output_description: |
  Summary of improvements applied to the target app: which files were modified,
  what was changed, and a suggestion for what to verify next.
finish_criteria:
  - All planned DSL file improvements have been written to disk
  - The improvement summary describes concrete changes made
  - Next verification steps are specified
---

prepare -> run_target
run_target -> analyze_execution
analyze_execution -> plan_improvements
plan_improvements -> apply_improvements
