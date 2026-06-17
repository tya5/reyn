---
title: Skill de-emphasis audit (user-facing positioning)
last_updated: 2026-06-14
status: proposal
audience: [maintainer]
---

# Skill de-emphasis audit

**Positioning principle (owner)**: in user-facing positioning, "skill" is an
*internal mechanism*, not a headline. The user-facing subject should be the
**capability / outcome** ("what you can do"), with skill mentioned as the
how, not the what. (This applies the spirit of `project_swe_skill_minimize_not_strengthen`
— skill is a means, not the product — to user-facing docs.)

This doc is a **proposal**: an audit of where user-facing docs currently lead
with "skill" as a headline, plus a reframe plan. Per positioning being
owner-principle territory, **no reframe is applied unilaterally** — this is for
owner + lead-coder review before any edit lands.

## Scope

**In scope** (user-facing): `README.md`, `docs/guide/for-users/`,
`docs/guide/getting-started/`, user-facing `docs/concepts/`.

**Out of scope** (audience is skill authors / internal — "skill" is the correct
subject there): `docs/guide/for-skill-authors/`, `docs/reference/`,
`docs/deep-dives/decisions/`, the `concepts/skills/` mechanism docs.

## Headline finding

**The high-leverage user-facing surfaces are already skill-de-emphasized.** The
prior general-agent positioning reframe (README rewrite) did the heavy lifting:

- `README.md` tagline leads with *"Self-hosted general agent — every decision
  constrained, auditable, replayable"*. The "Why Reyn" section leads with the
  OS-enforced-contract bet and the four guarantees (P3/P4, P5/P6, cost,
  credentials). Skill is **not** a headline; it appears only as one capability
  example (`### Write a reusable workflow (skill)` — already workflow-first).
- `docs/guide/for-users/index.md` leads with *"no skill authoring"* and a
  capability table ("What you can do in chat"). It explicitly frames skill as
  internal: *"Reyn routes your request to the right built-in skill automatically
  — you don't choose which one."* — skill as plumbing, exactly the desired framing.

So this audit is **not** a large rewrite. It confirms the surface is mostly
clean and flags a small number of lower-leverage residuals for owner judgment.

## Candidates (file → current → judgment)

| Location | Current | Judgment | Suggested reframe (if any) |
|---|---|---|---|
| `README.md:80` | `### Write a reusable workflow (skill)` | **Keep** | Already workflow-first; "(skill)" as the parenthetical mechanism is correct. No change. |
| `guide/for-users/index.md` | capability-table + "routes to the right built-in skill automatically" | **Keep** | Model example of the desired framing. No change. |
| `guide/getting-started/03-your-first-skill.md` (title "Your first skill") | tutorial title leads with "skill" | **Borderline / low priority** | This tutorial *teaches skill authoring*, so the audience is transitioning into authors — "skill" is arguably correct. Optional: "Your first reusable workflow" with "(skill)" subtitle, to keep the capability-first voice for readers still in the user funnel. Owner call. |
| `guide/for-users/work-with-files.md:111` (`## What the skill cannot do`) | section header names "the skill" | **Low priority** | Reframe to "What this cannot do" / "Limitations" — the user cares about the capability boundary, not that a skill implements it. |

## Recommendation

1. **No action required on the two high-leverage surfaces** (README, for-users
   index) — they already embody the principle; cite them as the reference voice.
2. **Optional, owner-judgment reframes** for the two low-leverage residuals
   above (getting-started title, work-with-files header). Each is a one-line
   change; neither is a positioning risk if left as-is.
3. **Standing guidance** for future user-facing docs (worth a short note in the
   docs style guide): lead sections with the *capability/outcome*; mention skill
   as the mechanism in a parenthetical or a "how it works" aside, never as the
   section's subject.

If owner approves any of the optional reframes, I'll apply them as a separate
small PR (one line each), keeping this audit doc as the rationale record.

## Related

- [reyn-differentiators.md](reyn-differentiators.md) — the OS-constant / skills-come-and-go lead differentiator (skill as one feature, not the headline)
- [phase-vs-skill-vs-os.md](../../../concepts/architecture/phase-vs-skill-vs-os.md) — the structural basis (OS-constant)
