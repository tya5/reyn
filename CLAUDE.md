# CLAUDE.md — Reyn Agent OS rules

Tier 1 rules only. Rationale, instances and measurements live in the linked
deep-dive docs — read those on demand, not every session.

**Editing this file** — every line loads into every session. Before adding
one, ask *would removing this cause a mistake?* If no, do not add it. **If CI
can catch the violation, write the gate, not a rule here.** Prose is how this
file grows: 1,310 → 2,443 words in three months, with no rules added at that
rate. Put `wc -w CLAUDE.md` in the PR that touches it.

## Constitution

> **Reyn is an operating system for LLM agents** — they decide, organize, and orchestrate; the OS makes every action typed, permissioned, audited, and recoverable by construction.

New features are read through **eight lenses** and must stand on the
**cross-cutting band**. Fail a band member and it does not ship.

**Lenses** — 1 System Design (responsibility at the right layer) · 2 Tool Contract (typed envelope, never a free-formed string) · 3 Retrieval (delivered deterministically, not stuffed into the prompt) · 4 Reliability (recovers; derived state survives WAL truncation) · 5 Security (permission-gated, sandbox-scoped) · 6 Evaluation (scorable in-run) · 7 Observability (audit-event trace sufficient to reconstruct) · 8 Product Think (predictable, cost-disciplined, legible).

**Band** — permission · audit-events · workspace-SSoT · crash-recovery (WAL) · cost/budget (bounding).

- **"event" is three things** — audit-event (`.reyn/events`) / WAL-event (`.reyn/state/wal.jsonl`) / hook-event. Never write bare "event".
- **Thin areas where new work is most valuable**: Retrieval and Evaluation.
- Full 8×7 table: `docs/concepts/architecture/charter.md`.

**The lenses gate features. These three gate everything else** — a changed
default, a new fallback, a new recovery command:

1. **Who stops this if it repeats?** Name the bounding subject, or there isn't one.
2. **Is this visible with the shipped config?** If seeing it requires changing a setting, it is not visible.
3. **Does the repair destroy the evidence?** If it does, say what survives it.

There is no fourth question for "what nobody wrote" — a checklist item asking
whether you considered it is answered "yes" every time.

## Hard rules

- **A doc describing a mechanism is stale the moment that mechanism's code — or a doc it mirrors — changes; fix it in the SAME PR.** Re-read the whole section, not just the line whose keyword you had in mind. **The reviewer owns this too**: a search that missed something cannot report that it missed, so the reviewer's value is a *different* query. Ask "what does this change make false?" before approving.
- **`docs/reference/runtime/control-ir.md` must stay synced with `OP_KIND_MODEL_MAP`** (`src/reyn/schemas/models.py`). New op kind → new section, same PR. **No CI checks this** — the CI-checked pair is `OP_KIND_MODEL_MAP` ↔ the `Op` union.
- **Audit-event kinds are a closed vocabulary.** Emitting a kind, declaring it in `AUDIT_EVENT_KINDS` (`src/reyn/core/events/event_schema.py`), and enumerating it in `docs/reference/runtime/events.md` is ONE three-part change. **CI checks the enumeration only — the semantic table row is on you.**
- **Recovery-feature PRs need a truncate-falsify test**: set X → truncate the WAL past X's events → reconstruct → assert X survives. Same PR.
- **`enforcement_self_test` (`src/reyn/security/sandbox/self_test.py`) stays 2-layer** (deny leg only, write + spawn axes). The richer per-axis contract is CI-conformance-only. Folding a new axis in needs an owner-level decision.

### TUI colour policy

Name the MEANING; the active Textual theme renders it. Never pick a colour
first (owner ruling #3525). **No terminal dependence** (owner ruling #4840):
reyn ships a full-colour theme, so there is nothing to defer to.

1. Meaning has a convention a reader already carries (*error*, *success*, *in-flight*) → map it to a Textual theme meaning: a theme token (`$error`, `$text-muted`, `$markdown-*` …) or an SGR `text-style`. Never an ANSI name.
2. Meaning is reyn-specific → any value that serves the design, full-colour included.

- **A colour is not a meaning.** "Error" is the meaning; red is one theme's rendering.
- **One thing is forbidden**: a literal in a widget stylesheet. Every value goes through a token in `src/reyn/interfaces/palette.py`, and stylesheets write a `@name@` marker.
- `tests/interfaces/test_tui_colour_tokens.py` fails on any colour value named outside the palette. Textual's own `DEFAULT_CSS` and `interfaces/web/` are out of scope.

## Testing policy (READ BEFORE WRITING TESTS)

Normative: **`docs/deep-dives/contributing/testing.ja.md`** (EN: `testing.md`).
For gate design and co-vet review also read
`docs/deep-dives/contributing/verification-hazards.md` — one root: **an
observation does not name its own referent**.

- Each test belongs to exactly one Tier (1 Contract / 2 OS invariant / 3 LLM-replay). Anything else is **Tier 4 — do not write**.
- First docstring line declares the Tier: `"""Tier 3a: ..."""`.
- Declaring a Tier presupposes a named behaviour that exists **outside the test's own docstring**.
- **Never fake a collaborator** when a real instance is cheaply constructible — no `MagicMock`/`AsyncMock`/`patch`, no hand-rolled stand-in. Use real instances or the `LLMReplay` Fake. **Cheap to construct is not the same as drivable**: a collaborator triggered only by its own timer may neither be faked nor waited for — give it an external drive (a `check()` you can call).
- **A test must not depend on private state** — not merely "never assert on it": naming a syntactic position only moves the read one line up. Use the public surface or a `snapshot()`-style read; if neither exists, that absence is the finding. The corpus does not comply yet (#4864) and nothing mechanical enforces this.
- **Never pin algorithm-level behaviour** — sort order, dict iteration order, cache structure, exact whitespace.
- **No snapshot/golden-file tests** outside `tests/scaffold/`.
- **A test writes no duration, in EITHER direction.** A duration is **rarely** the property under test; it usually stands in for an observation nobody exposed, so reaching for one says the seam is missing. When a duration genuinely IS the subject, the clock is an **input** you supply, never a `sleep` you wait out.
  - **Ceiling** (how long we will wait): no `@pytest.mark.timeout`, no `attempts=200`, no `range(N)` wrapping a wait. Wait on the condition unboundedly; CI's `--timeout=120` is the kill switch.
  - **Floor** (how long something must take): **no `sleep(N)` the assertion depends on** — not to outrun a threshold, not to let a task settle, not to let a clock tick (#4844). Inject the threshold or the clock. Splitting the decision out as a pure function removes the place a duration could be written at all.
- Tests for an extracted refactor live in `tests/scaffold/` with `triggered_by`/`removed_by`, and are **deleted in the PR that lands the refactor**.

## Comment policy (READ BEFORE WRITING OR MOVING A COMMENT)

Normative: **`docs/deep-dives/contributing/comments.md`** — classifies comments
by content (never by length), gives the one-question test for the class that
must stay inline regardless of size, and states why a residue reads "X breaks"
rather than "do not change this". This is a code-authoring policy; do not
conflate it with `verification-hazards.md`, which is about misreading a green.

## Test review — six questions, asked of the test's own code

**Read the tests in the diff, not the PR body's account of them.**
`test_tier_audit.py` matches the Tier line as a *string*; a declaration is not
a classification, and nothing else looks.

1. **Which Tier does it fit — 1, 2, 3, or none?** *If this assert fails, whose bug is it?* Two shapes that look like they fit and do not: a third party's property, a past bug's fingerprint.
2. **Is it the implementation, transcribed?** The same expression on both sides fails only when someone edits that line — and they will edit both.
3. **Who would miss this test if it were gone?** "Was handed X" is not a witness for "used X".
4. **Would it stay green having never run, or having run with nothing to bite on?** skip / collection error / zero collected / an assert over an **empty** collection all wear green's colour.
5. **What does it accumulate, and who bounds it?**
6. **Is the declared Tier the true one?** Only a human can answer this.

| | blocks when the answer is |
|---|---|
| 1 | **none** — including a third party's, a past bug's, and reyn's own trivia |
| 2 | yes — the same expression is on both sides |
| 3 | **nobody**, or only a configuration this test itself constructed |
| 4 | green having never run **or over an empty collection**, and the PR does not say so |
| 5 | **anything outside the test bounds it** — a thread, a timer, the caller's pace |
| 6 | the declared Tier is not the one question 1 named |

- 4 blocks on the **silence**, not on the skip: a whole-file skip is often correct; a green nobody qualified is not.
- 5 has no carve-out: "it is small today" is a measurement of today.
- 3 needs no accept-side exception — an accept-side test's consumer is the operators the gate would have false-positived against.
- **When something forces you to touch a test — a bump, a rebase, a CI failure — ask "should this exist" before "how do I make it pass again."**
- **Reviewer's note, on the PR**: record the answers per test before merging.

Instances and the full essays: `docs/deep-dives/contributing/test-review-six-questions.md`.

## PR workflow (READ BEFORE OPENING / REVIEWING A PR)

Every session authenticates as the same `gh` user — `--json author` cannot tell
sessions apart. Full rationale and the measured instances behind each rule:
**`docs/deep-dives/contributing/pr-workflow.md`**.

**Before you open a PR** — `ruff check .` (the same command CI runs, not a
narrower one), `python scripts/test_tier_audit.py --strict <changed test
files>`, `python scripts/verify_module_docstrings.py <changed src files>`,
`python scripts/mypy_ratchet.py`, `python scripts/flat_tests_ratchet.py`,
`python scripts/check_tests_path_literal_reference.py` (re-run right before
pushing — its baseline goes stale when `main` moves), and `pytest` **scoped to
your diff**. Do NOT run the full suite locally — CI does that in a clean
checkout. A green scoped `pytest` is **not** a green CI run.

Path-conditional gates:

- `docs/` → `mkdocs build --strict -f .mkdocs/mkdocs.yml && python scripts/check_doc_anchors.py && python scripts/check_retired_config_keys_denylist.py`, in that order (anchors needs the built `site/`).
- `src/reyn/mcp/` → `python scripts/check_fastmcp_import_boundary.py` (zero baseline: no `import fastmcp` under that directory).
- `tests/` → `python scripts/check_bare_tests_import_reference.py` and `python scripts/check_file_depth_reference.py`.

1. **Finish your own Test plan before merge.** Tick every Manual/Visual item or replace with `- [x] (skipped — <reason>)`. **Never tick a check that did not happen.** **Standing waiver, Visual items only** (owner 2026-08-08): merge with the Visual box unchecked — do not fabricate it.
2. **Role-prefix every issue / PR body / PR comment** — `**[lead-coder]** — `, etc. The PR body counts. This is the ONLY cross-session signal.
3. **Broker is a hint; the PR is the contract.** It is in-memory, lags ~30s, has no ack. Put critical pause/block signals on the PR **and** broker. **Deliver every new assignment over broker** — one written only on the issue reaches nobody.
4. **Never place a closing keyword next to an issue number** unless the PR really closes it — GitHub matches literally, negation and context included. Sub-PRs use `part of #X`; before writing `Closes #X`, enumerate open PRs with `part of #X`. The same matching applies to **commit messages** (a squash body concatenates them), and a body edit does not fix one.
5. **No blanket en/ja mirror obligation.** Fix a `.ja.md` file only when the PR falsifies something it asserts.
6. **Arc-closure remainder rule**: every remainder is **filed** or **explicitly dropped**, in the closing comment. "Next arc" is a third, silent state.
7. **A reviewer's blocking point goes in the PR body** as `- [ ] 🔴 <point>`, not only in a review comment — `--request-changes` does not work here. Rule 4 applies to that edit too.
8. **A PR touching `tests/` does not self-merge until a reviewer's TESTS-READ note lands on the PR.**
9. **Arming auto-merge ends your reading of that PR** — a checkbox added afterwards is invisible to the merge. Do not arm while a reviewer's point is open, and re-check the body before arming.

**Issue-triage label `blocked:external`** — needs owner judgment or an upstream
dependency. An open issue WITHOUT it is pickable by any peer session.

## Pre-conclusion observation checklist (READ BEFORE WRITING ANY FINDING / 結論)

**Trigger** — before writing: 結論 / conclusion / finding / 確定 / パターン /
一貫して / 100% / 全件 / N/N / 0% / all / every / proven / validated /
confirmed / attractor / hallucination / regression.

1. Can you list each specific observation supporting the claim?
2. Is each one **primary data** or **inference**? Inference downgrades "verified" to "hypothesised".
3. Did you look for data that **falsifies** it?
4. Is the observation infrastructure actually capturing what the claim needs?
5. For "N/N" or "100%": did you inspect each of the N, or 1-2 and extrapolate?

Re-frame instead of overstating: ❌ "X happens 100% in Y" → ✅ "Hypothesis: X may
dominate in Y. Direct verification: 1/N."

## When in doubt — read these

- **Verification hazards** (a green that means less, a red that overstates): `docs/deep-dives/contributing/verification-hazards.md`
- **Tier-1 rules, full rationale** (Constitution, hard rules, comment policy, pre-conclusion): `docs/deep-dives/contributing/tier1-rationale.md`
- **PR workflow, full rationale**: `docs/deep-dives/contributing/pr-workflow.md`
- **Six questions, full instances**: `docs/deep-dives/contributing/test-review-six-questions.md`
- **Workspace** (single source of truth): `docs/concepts/runtime/workspace.md`
- **Events / replay**: `docs/concepts/runtime/events.md`
- **`.reyn/` layout** (recovery-core vs persist/audit/cache, the write-gate): `docs/reference/runtime/reyn-dir-layout.md`
- **Permission model**: `docs/concepts/runtime/permission-model.md`
- **Op catalog and dispatch**: `src/reyn/core/op_runtime/`
- **Tool naming convention**: `docs/reference/runtime/tool-naming.md`
- **LLM trace analysis**: `docs/reference/dogfood-tracing.md` — `scripts/dogfood_trace.py --mode llm-payloads` is the entry point; do not hand-parse JSONL.
- **Full feature inventory**: `docs/feature-map.md`
