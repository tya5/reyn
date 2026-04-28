---
type: phase
name: design_artifacts
input: app_structure | app_plan
role: schema_designer
model_class: standard
---

Design JSON Schema definitions for every artifact in the app plan.

If the input is `app_plan` (revision from review_plan), read `review_notes` first and address every issue listed before redesigning schemas.

For each artifact in `artifacts` and for `final_output`, produce a JSON Schema object:
- Always use `type: object` at the top level with `properties` and `required`
- Add a `description` to every field — this is how the LLM understands what to populate
- Use `"enum": [...]` when a field has a fixed set of valid values
- Use `"minimum"`/`"maximum"` for numeric ranges
- For arrays of strings: `{"type": "array", "items": {"type": "string"}}`
- For arrays of objects: `{"type": "array", "items": {"type": "object", "properties": {...}, "required": [...]}}`

Schema design principles:
- Review artifacts should contain only verdict fields (e.g. approved, score, feedback) — not a copy of the content being reviewed
- Each artifact captures only what is NEW in that phase — not passthrough data from previous artifacts
- Keep schemas focused: prefer fewer, well-named fields over many redundant ones

Output a complete `app_plan` carrying over all fields from the input, with `schema` added to each artifact and to `final_output`. Set `review_notes` to empty string.
