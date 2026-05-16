---
type: how-to
topic: stdlib
audience: [human]
applies_to: [reyn/local/, reyn/project/]
---

# Import an existing skill

**Goal:** Bring a skill defined elsewhere (a prompt, a small script, another agent framework's spec) into reyn's DSL.

## When to use

- You have a working prompt or workflow in another tool and want to keep its behavior while gaining reyn's structure (validation, events, replay).
- You're rebuilding a one-shot prompt as a multi-phase reyn skill but want a draft to start from.

## Use the `skill_importer` stdlib skill

```bash
reyn run skill_importer "<paste your existing prompt or workflow description>"
```

`skill_importer` reads the input, infers a phase graph, and writes a draft skill into `reyn/local/<name>/`. It uses `lint` Control IR ops to verify its output before declaring success.

## Workflow

1. **Paste or describe.** Give `skill_importer` either the raw prompt text or a description of what the skill should do.
2. **Review the draft.** The importer writes `skill.md`, `phases/*.md`, and `artifacts/*.yaml` under `reyn/local/<name>/`.
3. **Lint.** `reyn lint <name>` should be clean. If not, the importer reports the issues at the end of the run.
4. **Run.** `reyn run <name> "<sample input>"`. Iterate the phase instructions if the output isn't right.
5. **Promote.** When happy, move from `reyn/local/` to `reyn/project/` to check it in.

## What the importer maps

| Source concept | reyn equivalent |
|----------------|-----------------|
| Single prompt | One phase + skill graph `entry → end` |
| "Step 1, then step 2" | Multiple phases connected linearly |
| "If X, do Y; otherwise Z" | A branching phase (`triage → [branchA, branchB]`) |
| Tool calls (read file, search) | Control IR ops |
| Repeated structured output | Artifact schema |

The importer doesn't always pick the cleanest decomposition. Use `skill_improver` to refine after import:

```bash
reyn run skill_improver "improve <name>" --allow-shell
```

## Promotion checklist

Before moving to `reyn/project/`:

- [ ] `reyn lint <name>` is clean.
- [ ] At least one happy-path eval case passes.
- [ ] `final_output` artifact matches what callers actually need.
- [ ] Phase instructions follow P8 (no schema enumeration, no Control IR syntax).

## See also

- [Reference: stdlib/skill_importer](../../reference/stdlib/skill_importer.md)
- [Reference: stdlib/skill_improver](../../reference/stdlib/skill_improver.md)
- [Reference: lint CLI](../../reference/cli/lint.md)
- [agent: skill_importer mapping rules](skill-importer-mapping.md)
