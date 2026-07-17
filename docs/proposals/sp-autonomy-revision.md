# System-prompt autonomy revision — proposal (for Fable5 review)

**Author:** lead-coder (Opus) · **Status:** DRAFT for Fable5 review · **Date:** 2026-07-05

## Goal
Raise reyn's agentic autonomy (self-direction) to competitor levels **without** removing
safety, and **without** SP bloat. Grounded in: (a) a code audit of reyn's current SP, and
(b) a sourced survey of competitor autonomy prompting (OpenAI "agentic eagerness" bundle;
Cursor / Codex / Cline / Windsurf / Devin / Gemini-CLI leaked prompts).

## The core design argument (why reyn can safely dial UP eagerness)
Prompt-only agents (Windsurf, Cline) must encode caution **in the prompt** — e.g. Windsurf's
"NEVER run an unsafe command automatically, even if the user overrides." reyn is different:
**destructive / unsafe boundaries are enforced structurally by the sandbox + permission layer,
regardless of what the SP says.** So reyn's SP can be *more* act-biased than a prompt-only agent
and delegate the "bound" to the permission gate. The autonomy lever is therefore low-risk here:
we relax the *asking* bias, not the *safety* bound.

Reference: OpenAI's own claim that a 3-sentence bundle (persistence + don't-guess tool-use +
planning) lifted internal SWE-bench Verified by ~20% — the only quantified prompt→behavior
evidence in the industry. reyn already has the planning + tool-use pieces; the gap is persistence
and (chiefly) an interactive **ask-first** bias competitors don't have.

---

## Change 1 (PRIMARY) — relax the interactive-mode clarification bias
**Location:** `src/reyn/tools/schemes/_universal_sp.py`, the `non_interactive`-gated fork (~L144-151).
**Only the interactive `else` branch changes; the `non_interactive` branch already proceeds.**

**BEFORE (interactive branch):**
> **Ambiguous or missing essential information** → ask ONE clarifying question instead of guessing.

**AFTER (interactive branch):**
> **Ambiguous or missing information** → default to proceeding: make the most reasonable
> assumption, state it explicitly, and continue. Ask a clarifying question ONLY when the
> ambiguity is BOTH consequential (a wrong guess causes real, hard-to-undo work) AND cannot be
> resolved from context or by inspecting the workspace. Prefer acting + documenting your
> assumption over asking.

**Why:** This is the single largest gap. reyn uniquely flips to ask-first in interactive mode;
Codex / Cursor / GPT-5 / Windsurf / Cline all bias to act even interactively ("decide the most
reasonable assumption, proceed with it, and document it for the user after you finish"). The
carve-out ("consequential AND unresolvable") preserves the good instinct for genuinely divergent
or irreversible cases — and those are *also* gated by the permission layer.

---

## Change 2 (SECONDARY) — close the persistence gap (anti-premature-yield)
**Location:** `src/reyn/runtime/router_system_prompt.py`, the finish-the-job Behaviour rule (~L209-213).
One appended clause — no new section.

**BEFORE (tail of the rule):**
> … keep working until you have actually produced the requested result, then report what real
> execution returned.

**AFTER (append):**
> … keep working until you have actually produced the requested result, then report what real
> execution returned. **Do not yield your turn with the task half-done to ask whether to continue —
> when you hit an obstacle, try an alternative before stopping; stop early only for a genuine
> blocker you cannot work around, and then report it honestly.**

**Why:** reyn's persistence is modest vs the competitor-canonical "keep going until the query is
completely resolved before ending your turn; only terminate when you are sure the problem is
solved" (GPT-5 / Cursor / Gemini-CLI verbatim). This closes it in one clause, reusing reyn's
existing honest-blocker framing so it stays consistent (no fabrication risk).

---

## Deliberately NOT changed
- **Discovery mandate** (`list_actions` before refusing) — already strong; keep.
- **Decomposition mandate** (`task__create` per target) — already strong; keep.
- **Anti-fabrication** ("errors surface verbatim") — keep; it is what makes aggressive persistence
  safe (persistence + honesty, never persistence + invention).
- **`non_claude` verify-before-acting / check-deps hygiene** — leave model-gated as-is. reyn
  deliberately drops these for Claude (tool layer handles structurally, #1791). Do NOT re-add
  (SP-minimize).
- The **`ROUTING RULE (ABSOLUTE)` "NO clarifying questions"** — already act-biased for the
  named-action fast-path; unchanged.

## SP-minimize compliance
Two surgical edits: one reworded fork (net +~30 words) + one appended clause (+~35 words). Zero new
sections. Consistent with `project_swe_skill_minimize_not_strengthen` (change by relaxing/rewording,
not by piling on new directives).

## Open questions for Fable5
1. Change 1: should the "proceed" instruction **explicitly** name the permission layer as the reason
   it's safe (e.g. "destructive actions are gated regardless"), or is that over-explaining to the model?
2. Is the persistence clause (Change 2) redundant with the existing anti-stub sentence, or does the
   explicit "don't yield to ask whether to continue" add real signal? (Competitor prompts keep both;
   ablation evidence is OpenAI-internal only.)
3. Should any of this be model-gated (Claude vs non-Claude) like the existing hygiene block, or is
   act-bias model-agnostic?
4. Interactive UX risk: does relaxing ask-first hurt the chat experience for genuinely exploratory
   "how should I approach X" questions (where Claude Code deliberately answers-first)? Consider a
   carve-out for "the user asked HOW/whether, not to DO."

---

# Fable5 review (2026-07-05)

**Verdict: APPROVE both changes, with three amendments.** The diagnosis is verified against the
code (`_universal_sp.py` L144-151 fork; `router_system_prompt.py` finish-the-job rule), the changes
are genuinely surgical, and the core argument — reyn's structural sandbox/permission gates let the
SP be more act-biased than prompt-only agents — is the proposal's strongest and soundest insight.

## Amendments (required before implementation)

**A1 — Change 1: restore the ONE-question discipline in the rare-ask path.** The BEFORE text's
"ask ONE" cap is good and gets lost in the rewrite. When the consequential+unresolvable carve-out
fires, the model should ask exactly **one targeted** question, never a questionnaire.

**A2 — Change 1: add the question-shaped-request carve-out (resolves open Q4: YES).** Without it,
"prefer acting over asking" makes the model DO things when the user asked HOW to do them. Claude
Code's answer-first rule here is correct interactive UX, not timidity — keep that one behavior.

**Revised AFTER text for Change 1 (supersedes the draft's):**
> **Ambiguous or missing information** → default to proceeding: make the most reasonable
> assumption, state it explicitly, and continue. Ask ONE targeted clarifying question ONLY when
> the ambiguity is BOTH consequential (a wrong guess causes real, hard-to-undo work) AND cannot
> be resolved from context or by inspecting the workspace.
> When the user asks HOW to approach something, or whether to do it, answer the question first —
> do not jump straight into taking actions they haven't asked for.

**A3 — add a verification plan (the draft's real gap).** Land with evidence, not vibes: a dogfood
before/after on interactive scenarios measuring (a) clarifying-question rate on resolvable
ambiguity, (b) tasks completed without a mid-task "続けますか?" yield, (c) no regression in
destructive-op gating (the permission layer should show unchanged deny counts). Use the existing
`dogfood_trace` tooling; hypothesis→verify before any batch claim.

## Rulings on the open questions
- **Q1: NO.** Don't name the permission layer in the SP — it's rationale for humans, adds tokens,
  and invites boundary-probing. Keep it in this doc only.
- **Q2: KEEP Change 2.** Not redundant: anti-stub ("don't stop at a plan/stub") and
  anti-permission-seeking ("don't yield to ask whether to continue") are distinct failure modes.
  Competitor prompts carry both.
- **Q3: ship UNGATED.** Competitors use identical act-bias text across models; reyn's weak-tier
  discipline is subtract-not-add and this is a rewording, not an addition. Watch the existing
  post-measurement dogfood loop for weak-model regressions rather than pre-gating.
- **Q4: resolved by A2** (carve-out added).

## Out of scope, noted for later
`max_iterations=5` (router_loop default) is the remaining **non-SP** autonomy limiter. If the SP
changes land and perceived autonomy is still low, that runtime knob — not more SP text — is the
next lever. Do not bundle it into this change (separate measurement).

---

# Fable5 revised draft — AUTHORITATIVE, implementation-ready (2026-07-05)

This section supersedes the Opus draft's AFTER texts. It incorporates review amendments A1/A2
and tightens Change 2 to the competitor-canonical persistence shape. The applying session (Opus)
should implement exactly this.

## Edit 1 — `src/reyn/tools/schemes/_universal_sp.py`, interactive branch of the ambiguity fork (~L149-150)

The `non_interactive` branch is **unchanged** (it already proceeds unconditionally — correct,
since there is no user to ask). Only the interactive `else` string is replaced. The fork itself
stays (the two branches remain deliberately different: unconditional-proceed vs bounded-proceed).

**REPLACE the interactive branch text:**
> **Ambiguous or missing essential information** → ask ONE clarifying question instead of guessing.

**WITH:**
> **Ambiguous or missing information** → default to proceeding: make the most reasonable
> assumption, state it explicitly, and continue. Ask ONE targeted clarifying question ONLY when
> the ambiguity is BOTH consequential (a wrong guess causes real, hard-to-undo work) AND cannot
> be resolved from context or by inspecting the workspace. When the user asks HOW to approach
> something, or whether to do it, answer first — do not jump into actions they haven't asked for.

## Edit 2 — `src/reyn/runtime/router_system_prompt.py`, finish-the-job Behaviour item (~L209-213)

**APPEND to the existing rule** (after "…then report what real execution returned."):
> Only end your turn when the request is fully resolved or genuinely blocked: never yield
> half-done to ask whether to continue, and when an approach fails, try an alternative before
> stopping. If a real blocker remains, report it honestly.

Rationale for the tightening vs the Opus draft: this is the canonical persistence shape
("only terminate when resolved") fused with reyn's existing honest-blocker framing in two
sentences; the Opus version said the same in a wordier, more hedged way.

## Application notes for the implementing session
1. **Where:** exactly the two strings above; no other SP text changes. SP-minimize holds
   (net ≈ +60 words across the whole SP).
2. **Tests:** run full `pytest` — any Tier-1 contract test or scaffold snapshot asserting the old
   "ask ONE clarifying question" wording must be updated in the same PR (grep tests for that
   phrase). Do NOT add new SP-text-pinning tests (Tier-4 format pins).
3. **Cache note:** both edits are in the static/cached SP prefix — expect a one-time prompt-cache
   invalidation, no structural impact.
4. **Verification (required, amendment A3):** dogfood before/after on interactive scenarios —
   (a) clarifying-question rate on resolvable ambiguity ↓, (b) mid-task "should I continue?"
   yields ↓, (c) permission-layer deny counts unchanged. Use `dogfood_trace`; verify before any
   batch claim.
5. **Docs:** if any user-facing doc describes the interactive ask-first behavior, mirror it in the
   same PR and ping docs-maintainer on land.
