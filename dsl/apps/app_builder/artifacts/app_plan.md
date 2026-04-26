---
type: artifact
name: app_plan
---

# Structured plan produced by plan_app.

app_name: string
app_path: string
entry_phase: string
finish_criteria: string[]
phases:
  type: array
  items:
    type: object
    properties:
      name:
        type: string
      role:
        type: string
      input_artifact:
        type: string
      input_description:
        type: string
      instructions:
        type: string
      can_finish:
        type: boolean
    required: [name, role, input_artifact, input_description, instructions]
transitions:
  type: array
  items:
    type: object
    properties:
      from:
        type: string
      to:
        type: array
        items:
          type: string
    required: [from, to]
artifacts:
  type: array
  items:
    type: object
    properties:
      name:
        type: string
      fields:
        type: array
        items:
          type: object
          properties:
            name:
              type: string
            type:
              type: string
          required: [name, type]
    required: [name, fields]
final_output:
  type: object
  properties:
    name:
      type: string
    description:
      type: string
    fields:
      type: array
      items:
        type: object
        properties:
          name:
            type: string
          type:
            type: string
        required: [name, type]
  required: [name, description, fields]
