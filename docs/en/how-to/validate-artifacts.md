---
type: how-to
topic: dsl
audience: [human]
applies_to: [phases/*.md]
---

# Validate an artifact before the LLM sees it

**Goal:** Catch malformed input before the LLM is called, and surface findings to the LLM so it can react (ask the user, reject, fall back).

## When to use

- The input artifact has a structure that's expensive for the LLM to vet (long lists, nested objects, optional fields with cross-field constraints).
- You want a deterministic gate, not "the LLM will probably notice."

## Pattern

Add a `validate` step to the phase's preprocessor. Findings land at `into`; the LLM reads them like any other input field.

```yaml
---
type: phase
name: triage
input: ticket_batch
preprocessor:
  - validate:
      schema:
        type: object
        required: [tickets]
        properties:
          tickets:
            type: array
            items:
              type: object
              required: [id, title]
              properties:
                id: { type: string }
                title: { type: string, minLength: 1 }
      target: input
      into: validation_findings
---

Triage each ticket into `bug`, `feature`, or `chore`. If
`validation_findings.errors` is non-empty, ask the user to fix the
input before triaging.
```

## What `validation_findings` looks like

```json
{
  "errors":   [{"path": "tickets[3].title", "message": "must NOT be shorter than 1 chars"}],
  "warnings": [],
  "valid":    false
}
```

The LLM reads it under whatever key you set in `into`. Phase instructions reference findings by name only — they do not need to know the schema (P8).

## Where validation already happens for free

You don't need a `validate` step for either of these — the OS does it:

- **Transition validation.** Every artifact is validated against the next phase's `input` schema before the transition.
- **Final output validation.** The terminating artifact is validated against the skill's `final_output_schema`.

Use `validate` only when you want validation **inside** the phase, before the LLM call.

## See also

- [Reference: preprocessor](../reference/dsl/preprocessor.md) — `validate` step
- [Reference: artifact.yaml](../reference/dsl/artifact-yaml.md)
- [compose-skills-with-run-skill.md](compose-skills-with-run-skill.md)
