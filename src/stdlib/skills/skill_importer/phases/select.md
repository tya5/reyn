---
type: phase
name: select
input: candidate_list
role: skill_selector
can_finish: false
max_act_turns: 3
---

Pick exactly one candidate, asking the user when the choice isn't obvious.

## Branching by candidate count

- **0 candidates** — emit a control.type='abort' decide turn.
  control.reason.summary: "no skills matched query '<query>' in <registry_url>".
  Suggest the user broaden their query or pick a different registry.

- **Exactly 1 candidate** — auto-select it. Skip the ask_user op. Emit a
  decide turn transitioning to `convert` with that candidate filled into
  `selected_candidate`.

- **2+ candidates** — emit an `ask_user` op showing the candidates as a
  numbered menu:

  ```
  Found N candidates for "<query>":
    1) <name>  — <summary>
    2) <name>  — <summary>
    ...

  Which one would you like to import? (1-N, or 'cancel' to abort)
  ```

  Pass the names as `suggestions: [...]` so the chat UI can hint them.

  Parse the user's reply:
  - A number → the corresponding candidate
  - The skill's name (case-insensitive substring match) → that candidate
  - "cancel" / empty → emit an `abort` decide turn

## Decide turn

When you have a chosen candidate, emit `selected_candidate` with `query`,
`name`, `summary`, and `source_url` copied from the chosen item.
Transition to `convert`.

## Constraints

- Only choose from the `candidates` list you were given. Do not search again.
- If the user's answer doesn't match any candidate, ask once more before
  aborting (use a second `ask_user`). Don't loop forever.
