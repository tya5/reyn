# CLAUDE.md — Reyn Agent OS rules

Tier 1 rules only. Rationale, instances and measurements live in the linked
deep-dive docs — read those on demand, not every session.

**Editing this file** — every line loads into every session. Before adding
one, ask two questions. *Would removing this cause a mistake?* If no, do not
add it. **And: what act fires it?** Name that act in the line itself — a rule
whose reader has to already suspect it is missing will not be read, because
the failures it prevents are the ones that remove the suspicion. A rule with
no trigger is not a rule; it is a line someone can quote after the fact.
**If CI can catch the violation, write the gate, not a rule here.** Prose is
how this file grows: 1,310 → 2,443 words in three months, with no rules added at that
rate. Put `wc -w CLAUDE.md` in the PR that touches it. **A rule that binds one
directory belongs in that directory's own `CLAUDE.md`, not here** — it loads
when someone opens those files and costs nothing to everyone else.

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
- **Recovery-feature PRs need a truncate-falsify test**: set X → truncate the WAL past X's events → reconstruct → assert X survives. Same PR.

### TUI colour policy

Rules for colour live in `src/reyn/interfaces/CLAUDE.md`, next to the code they
bind (owner ruling: an unrelated session should not carry them).

## Testing policy (READ BEFORE WRITING TESTS)

Normative: **`docs/deep-dives/contributing/testing.ja.md`** (EN: `testing.md`).
For gate design and co-vet review also read
`docs/deep-dives/contributing/verification-hazards.md` — one root: **an
observation does not name its own referent**.

- Each test belongs to exactly one Tier (1 Contract / 2 OS invariant / 3 LLM-replay). Anything else is **Tier 4 — do not write**.
- First docstring line declares the Tier: `"""Tier 3a: ..."""`.
- Declaring a Tier presupposes a named behaviour that exists **outside the test's own docstring**.
- **Never fake a collaborator** when a real instance is cheaply constructible — no `MagicMock`/`AsyncMock`/`patch`, no hand-rolled stand-in. Use real instances or the `LLMReplay` Fake. **Cheap to construct is not the same as drivable**: a collaborator triggered only by its own timer may neither be faked nor waited for — give it an external drive (a `check()` you can call).
- **A test must not depend on private state** — not merely "never assert on it": naming a syntactic position only moves the read one line up. Use the public surface or a `snapshot()`-style read; if neither exists, that absence is the finding. `test_tier_audit.py`'s Rule 8 (#4864) now enforces this mechanically — disclosed gaps, not a claim of total coverage (`docs/reference/test-tier-audit.md`).
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
| 4 | green having never run **or over an empty collection**, and the test's own docstring does not say so |
| 5 | **anything outside the test bounds it** — a thread, a timer, the caller's pace |
| 6 | the declared Tier is not the one question 1 named |

- 4 blocks on the **silence**, not on the skip: a whole-file skip is often correct; a green nobody qualified is not.
- 4's disclosure lives in the **test's own docstring**, not the PR: a PR is read once, at merge time; the next person to touch the test opens the file, not the PR.
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
- `tests/` → `python scripts/check_bare_tests_import_reference.py`, `python scripts/check_file_depth_reference.py`, and `python scripts/check_subprocess_reyn_pin.py`.

1. **Finish your own Test plan before merge.** Tick every Manual/Visual item or replace with `- [x] (skipped — <reason>)`. **Never tick a check that did not happen.** **Standing waiver, Visual items only** (owner 2026-08-08): merge with the Visual box unchecked — do not fabricate it.
2. **Role-prefix every issue / PR body / PR comment** — `**[lead-coder]** — `, etc. The PR body counts. This is the ONLY cross-session signal.
3. **Broker is a hint; the PR is the contract.** It is in-memory, lags ~30s, has no ack. Put critical pause/block signals on the PR **and** broker. **Deliver every new assignment over broker** — one written only on the issue reaches nobody.
4. **Never place a closing keyword next to an issue number** unless the PR really closes it — GitHub matches literally, negation and context included. Sub-PRs use `part of #X`; before writing `Closes #X`, enumerate open PRs with `part of #X`. The same matching applies to **commit messages and the PR title** (a squash body concatenates them), and a body edit does not fix one.
5. **No blanket en/ja mirror obligation.** Fix a `.ja.md` file only when the PR falsifies something it asserts.
6. **Arc-closure remainder rule**: every remainder is **filed** or **explicitly dropped**, in the closing comment. "Next arc" is a third, silent state. (`docs/deep-dives/contributing/issue-management.md`)
7. **A reviewer's blocking point goes in the PR body** as `- [ ] 🔴 <point>` — and closing it takes a comment quoting that line verbatim; ticking alone leaves no record and the gate stays red (#5314). A `BLOCKING (head <sha>)` / `BLOCKING-CLEARED (head <sha>)` comment pair is the equivalent comment-only form. Rule 4 applies to that edit too.
   - Verbatim has two different targets: `BLOCKING-CLEARED` quotes the `BLOCKING` comment's **identifying line** (its first non-empty line after the marker), never a summary; a checked `- [x] 🔴` needs a comment quoting **that line itself**, not restated in the body. One comment can cover several checked lines.
8. **A PR touching `tests/` does not self-merge until a reviewer's TESTS-READ claim lands as a comment's FIRST line** (marker + head SHA together; grounds go from line 2 on).
9. **`update-branch` only on a real conflict** (`mergeable=CONFLICTING`), and **never re-run a gate by hand** — a merely-behind branch is fine as it is, both PR pushes and comments already trigger the gates that read them, and each needless run costs a full matrix on a saturable resource (#4239).
10. **Arming auto-merge ends your reading of that PR** — a checkbox added afterwards is invisible to the merge. Do not arm while a reviewer's point is open, and re-check the body before arming.

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

## Read these — each line says WHEN it fires

A rule nobody is told to read is not a rule. Every entry below names the act
that triggers it; "when in doubt" is not a trigger, because the failures these
prevent are the ones that remove the doubt.

- **Before filing an issue** — `docs/deep-dives/contributing/issue-management.md`. An issue gets its axis label(s) when it is **filed**, not later; no axis label means "not yet judged", so an unlabelled backlog carries no order to dispatch by.
- **Before reading a green or a red as evidence** — `docs/deep-dives/contributing/verification-hazards.md`
- **Before writing a review's blocking point** — `docs/deep-dives/contributing/test-review-six-questions.md`
- **When a Tier-1 rule above seems wrong or costly** — `docs/deep-dives/contributing/tier1-rationale.md`; **PR workflow's** own rationale: `docs/deep-dives/contributing/pr-workflow.md`
- **When you touch session/agent state on disk** — `docs/concepts/runtime/workspace.md`; **`.reyn/` layout** (recovery-core vs persist/audit/cache, the write-gate): `docs/reference/runtime/reyn-dir-layout.md`
- **When you emit, read, or replay an audit-event** — `docs/concepts/runtime/events.md`
- **When you add or change a permission decision** — `docs/concepts/runtime/permission-model.md`
- **When you add or rename an op or tool** — `src/reyn/core/op_runtime/` (catalog and dispatch); naming: `docs/reference/runtime/tool-naming.md`
- **When you analyse an LLM trace** — `docs/reference/dogfood-tracing.md`; `scripts/dogfood_trace.py --mode llm-payloads` is the entry point, do not hand-parse JSONL.
- **When you claim a feature does or does not exist** — `docs/feature-map.md`
