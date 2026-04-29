---
type: eval
app: src/stdlib/apps/app_builder/app.md
dsl_root: src/stdlib/
judge_model: openai/gemini-2.5-flash-lite
---

## case: simple_article_app
input: "Build an app that writes an article about the benefits of AI and then reviews it for clarity and accuracy."

### phase: plan_app
schema:
  type: object
  required:
    - app_name
    - app_description
    - app_path
    - entry_phase
    - finish_criteria
    - phases
    - transitions
    - artifacts
    - final_output
  properties:
    app_name:
      type: string
    app_description:
      type: string
    app_path:
      type: string
    entry_phase:
      type: string
    finish_criteria:
      type: array
      minItems: 2
      items:
        type: string
    phases:
      type: array
      minItems: 1
      items:
        type: object
        required:
          - name
          - role
          - input_artifact
          - instructions
        properties:
          name:
            type: string
          role:
            type: string
          model_class:
            type: string
            enum:
              - light
              - standard
              - strong
          input_artifact:
            type: string
          instructions:
            type: string
          can_finish:
            type: boolean
    transitions:
      type: array
      minItems: 1
      items:
        type: object
        required:
          - from
          - to
        properties:
          from:
            type: string
          to:
            type: array
            minItems: 1
            items:
              type: string
    artifacts:
      type: array
      items:
        type: object
        required:
          - name
          - description
          - schema
        properties:
          name:
            type: string
          description:
            type: string
          schema:
            type: object
            required:
              - type
              - properties
            properties:
              type:
                type: string
                enum:
                  - object
              properties:
                type: object
    final_output:
      type: object
      required:
        - name
        - description
        - schema
      properties:
        name:
          type: string
        description:
          type: string
        schema:
          type: object
          required:
            - type
            - properties
          properties:
            type:
              type: string
              enum:
                - object
            properties:
              type: object

quality:
- [required] The app_name is a valid snake_case identifier.
- [required] The app_description is a concise, single sentence.
- [required] Phase instructions are domain-logic specific to the target app and do not include meta-instructions for app_builder itself.
- [required] Review phase instructions clearly state quality criteria and rejection conditions, including the rollback clause.

### phase: design_artifacts
schema:
  type: object
  required:
    - artifacts
    - final_output
  properties:
    artifacts:
      type: array
      items:
        type: object
        required:
          - schema
        properties:
          schema:
            type: object
            required:
              - type
              - properties
            properties:
              type:
                type: string
                enum:
                  - object
              properties:
                type: object
    final_output:
      type: object
      required:
        - schema
      properties:
        schema:
          type: object
          required:
            - type
            - properties
          properties:
            type:
              type: string
              enum:
                - object
            properties:
              type: object

quality:
- [required] Intermediate artifact schemas contain all necessary context for reviewers.
- [required] Review verdict artifact schemas contain only verdict fields, not duplicated content.
- [required] Final output schemas accurately reflect the deliverable, including approved content and verdict fields if applicable.

### phase: review_plan
schema:
  type: object
  properties: {}

quality:
- [required] If rollback is chosen, control.reason.summary lists all identified issues as a numbered list.
- [required] If transition is chosen, the app_plan artifact is identical to the input.

### phase: build_app
schema:
  type: object
  required:
    - app_name
    - app_path
    - files_written
    - file_count
  properties:
    app_name:
      type: string
    app_path:
      type: string
    files_written:
      type: array
      minItems: 1
      items:
        type: string
    file_count:
      type: integer
      minimum: 0

quality:
- [required] All generated files (.md, .yaml) have correct frontmatter delimiters.
- [required] Phase files correctly reference input artifacts or stdlib artifacts like user_message.
- [required] Artifact files correctly define schemas based on the app_plan.
- [required] The generated app.md includes a correct graph structure derived from transitions.

### phase: verify_app
schema:
  type: object
  required:
    - lint_passed
    - lint_issues
    - app_name
    - app_path
    - files_written
    - file_count
    - summary
  properties:
    lint_passed:
      type: boolean
    lint_issues:
      type: array
      items:
        type: string
    app_name:
      type: string
    app_path:
      type: string
    files_written:
      type: array
      items:
        type: string
    file_count:
      type: integer
    summary:
      type: string

quality:
- [required] If lint_passed is false, control.type is 'rollback' and control.reason.summary lists verbatim lint issues.
- [required] If lint_passed is true, control.type is 'finish'.
- [required] The summary field accurately describes the generated app's purpose for its users, not the build process.

### cross_phase
- [required] plan_app.app_name == build_app.app_name
- [required] plan_app.app_path == build_app.app_path
- [required] build_app.files_written == verify_app.files_written
- [required] build_app.file_count == verify_app.file_count

### final
schema:
  type: object
  required:
    - app_name
    - app_path
    - files_written
    - file_count
    - lint_passed
    - lint_issues
    - summary
  properties:
    app_name:
      type: string
    app_path:
      type: string
    files_written:
      type: array
      minItems: 1
      items:
        type: string
    file_count:
      type: integer
      minimum: 0
    lint_passed:
      type: boolean
    lint_issues:
      type: array
      items:
        type: string
    summary:
      type: string

quality:
- [required] The summary field describes the generated app's purpose for its users.
- [required] If lint_passed is false, the lint_issues array contains specific errors that need fixing.

## case: complex_research_app
input: "Create an app to research and summarize the latest advancements in renewable energy, requiring a structured research phase before generation and a final review."

### phase: plan_app
schema:
  type: object
  required:
    - app_name
    - app_description
    - app_path
    - entry_phase
    - finish_criteria
    - phases
    - transitions
    - artifacts
    - final_output
  properties:
    app_name:
      type: string
    app_description:
      type: string
    app_path:
      type: string
    entry_phase:
      type: string
    finish_criteria:
      type: array
      minItems: 2
      items:
        type: string
    phases:
      type: array
      minItems: 1
      items:
        type: object
        required:
          - name
          - role
          - input_artifact
          - instructions
        properties:
          name:
            type: string
          role:
            type: string
          model_class:
            type: string
            enum:
              - light
              - standard
              - strong
          input_artifact:
            type: string
          instructions:
            type: string
          can_finish:
            type: boolean
    transitions:
      type: array
      minItems: 1
      items:
        type: object
        required:
          - from
          - to
        properties:
          from:
            type: string
          to:
            type: array
            minItems: 1
            items:
              type: string
    artifacts:
      type: array
      items:
        type: object
        required:
          - name
          - description
          - schema
        properties:
          name:
            type: string
          description:
            type: string
          schema:
            type: object
            required:
              - type
              - properties
            properties:
              type:
                type: string
                enum:
                  - object
              properties:
                type: object
    final_output:
      type: object
      required:
        - name
        - description
        - schema
      properties:
        name:
          type: string
        description:
          type: string
        schema:
          type: object
          required:
            - type
            - properties
          properties:
            type:
              type: string
              enum:
                - object
            properties:
              type: object

quality:
- [required] The app_name is a valid snake_case identifier.
- [required] The app_description is a concise, single sentence.
- [required] Phase instructions are domain-logic specific to the target app and do not include meta-instructions for app_builder itself.
- [required] Review phase instructions clearly state quality criteria and rejection conditions, including the rollback clause.

### phase: design_artifacts
schema:
  type: object
  required:
    - artifacts
    - final_output
  properties:
    artifacts:
      type: array
      items:
        type: object
        required:
          - schema
        properties:
          schema:
            type: object
            required:
              - type
              - properties
            properties:
              type:
                type: string
                enum:
                  - object
              properties:
                type: object
    final_output:
      type: object
      required:
        - schema
      properties:
        schema:
          type: object
          required:
            - type
            - properties
          properties:
            type:
              type: string
              enum:
                - object
            properties:
              type: object

quality:
- [required] Intermediate artifact schemas contain all necessary context for reviewers.
- [required] Review verdict artifact schemas contain only verdict fields, not duplicated content.
- [required] Final output schemas accurately reflect the deliverable, including approved content and verdict fields if applicable.

### phase: review_plan
schema:
  type: object
  properties: {}

quality:
- [required] If rollback is chosen, control.reason.summary lists all identified issues as a numbered list.
- [required] If transition is chosen, the app_plan artifact is identical to the input.

### phase: build_app
schema:
  type: object
  required:
    - app_name
    - app_path
    - files_written
    - file_count
  properties:
    app_name:
      type: string
    app_path:
      type: string
    files_written:
      type: array
      minItems: 1
      items:
        type: string
    file_count:
      type: integer
      minimum: 0

quality:
- [required] All generated files (.md, .yaml) have correct frontmatter delimiters.
- [required] Phase files correctly reference input artifacts or stdlib artifacts like user_message.
- [required] Artifact files correctly define schemas based on the app_plan.
- [required] The generated app.md includes a correct graph structure derived from transitions.

### phase: verify_app
schema:
  type: object
  required:
    - lint_passed
    - lint_issues
    - app_name
    - app_path
    - files_written
    - file_count
    - summary
  properties:
    lint_passed:
      type: boolean
    lint_issues:
      type: array
      items:
        type: string
    app_name:
      type: string
    app_path:
      type: string
    files_written:
      type: array
      items:
        type: string
    file_count:
      type: integer
    summary:
      type: string

quality:
- [required] If lint_passed is false, control.type is 'rollback' and control.reason.summary lists verbatim lint issues.
- [required] If lint_passed is true, control.type is 'finish'.
- [required] The summary field accurately describes the generated app's purpose for its users, not the build process.

### cross_phase
- [required] plan_app.app_name == build_app.app_name
- [required] plan_app.app_path == build_app.app_path
- [required] build_app.files_written == verify_app.files_written
- [required] build_app.file_count == verify_app.file_count

### final
schema:
  type: object
  required:
    - app_name
    - app_path
    - files_written
    - file_count
    - lint_passed
    - lint_issues
    - summary
  properties:
    app_name:
      type: string
    app_path:
      type: string
    files_written:
      type: array
      minItems: 1
      items:
        type: string
    file_count:
      type: integer
      minimum: 0
    lint_passed:
      type: boolean
    lint_issues:
      type: array
      items:
        type: string
    summary:
      type: string

quality:
- [required] The summary field describes the generated app's purpose for its users.
- [required] If lint_passed is false, the lint_issues array contains specific errors that need fixing.
