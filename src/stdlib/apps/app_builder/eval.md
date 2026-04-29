---
type: eval
app: src/stdlib/apps/app_builder/app.md
dsl_root: src/
---

## case: simple_article_writer
input: "Build an app that writes a short article about a given topic."

### phase: plan_app
quality:
- The generated app_name is in snake_case.
- The app_description accurately summarizes the app's purpose in one sentence.
- The app_path is correctly formatted as 'reyn/local/{app_name}'.
- The entry_phase is correctly identified and matches a phase name.
- finish_criteria are relevant and clearly stated.
- All phases in the `phases` list have unique names and valid `role` and `model_class` values.
- Phase instructions focus solely on the target app's domain logic, not on app-builder concerns.
- Transitions correctly define the graph flow, with `to` values being only phase names.
- Artifacts list includes all artifacts used as input by phases, with clear descriptions.
- Final output artifact is well-defined with a name and description.

### phase: design_artifacts
quality:
- All artifact schemas have `type: object` with `properties` and `required` fields.
- Every property within each schema has a `description` field.
- Required fields are correctly declared for each artifact schema.
- Schemas for review phases contain only verdict fields, not duplicated content.
- Final output artifact schema includes deliverable content and necessary verdict fields.
- Schemas are focused and avoid redundant fields.

### phase: review_plan
quality:
- The reviewer correctly identifies schema issues based on the defined criteria.
- If issues are found, `control.type='rollback'` is emitted with a clear `control.reason.summary` listing numbered problems.
- If no issues are found, the phase transitions to `build_app` with the `app_plan` artifact unchanged.

### phase: build_app
quality:
- app.md is correctly generated with all required fields (name, description, entry, final_output, finish_criteria).
- The graph in app.md correctly reflects the transitions defined in the plan.
- Phase files correctly include `type: phase`, `name`, `input`, `role`, `model_class` (if applicable), and `can_finish` (if applicable).
- Phase instructions are copied verbatim from the plan.
- Artifact files are correctly formatted YAML with `name`, `description`, and `schema`.
- All artifact files and the final output artifact file are written.
- The `build_result` artifact is generated correctly after all files are written.

### phase: verify_app
quality:
- The lint op is executed with the correct `app_path`.
- If lint fails, `control.type='rollback'` is emitted with lint issues included in the reason.
- If lint passes, the phase finishes with a correctly structured `app_builder_result` artifact.
- The `summary` field in `app_builder_result` describes what the generated app does for its users, not the build process itself.

## case: complex_review_and_revision_app
input: "Create an app that generates a legal disclaimer and allows a reviewer to approve or reject it with specific feedback, looping back for revisions."

### phase: plan_app
quality:
- The generated app_name is in snake_case.
- The app_description accurately summarizes the app's purpose in one sentence.
- The app_path is correctly formatted as 'reyn/local/{app_name}'.
- The entry_phase is correctly identified and matches a phase name.
- finish_criteria are relevant and clearly stated.
- All phases in the `phases` list have unique names and valid `role` and `model_class` values.
- Phase instructions focus solely on the target app's domain logic, not on app-builder concerns.
- Transitions correctly define the graph flow, with `to` values being only phase names.
- Artifacts list includes all artifacts used as input by phases, with clear descriptions.
- Final output artifact is well-defined with a name and description.

### phase: design_artifacts
quality:
- All artifact schemas have `type: object` with `properties` and `required` fields.
- Every property within each schema has a `description` field.
- Required fields are correctly declared for each artifact schema.
- Schemas for review phases contain only verdict fields, not duplicated content.
- Final output artifact schema includes deliverable content and necessary verdict fields.
- Schemas are focused and avoid redundant fields.

### phase: review_plan
quality:
- The reviewer correctly identifies schema issues based on the defined criteria.
- If issues are found, `control.type='rollback'` is emitted with a clear `control.reason.summary` listing numbered problems.
- If no issues are found, the phase transitions to `build_app` with the `app_plan` artifact unchanged.

### phase: build_app
quality:
- app.md is correctly generated with all required fields (name, description, entry, final_output, finish_criteria).
- The graph in app.md correctly reflects the transitions defined in the plan.
- Phase files correctly include `type: phase`, `name`, `input`, `role`, `model_class` (if applicable), and `can_finish` (if applicable).
- Phase instructions are copied verbatim from the plan.
- Artifact files are correctly formatted YAML with `name`, `description`, and `schema`.
- All artifact files and the final output artifact file are written.
- The `build_result` artifact is generated correctly after all files are written.

### phase: verify_app
quality:
- The lint op is executed with the correct `app_path`.
- If lint fails, `control.type='rollback'` is emitted with lint issues included in the reason.
- If lint passes, the phase finishes with a correctly structured `app_builder_result` artifact.
- The `summary` field in `app_builder_result` describes what the generated app does for its users, not the build process itself.
