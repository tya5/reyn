# Tier-1 rules — full rationale

Normative form lives in `CLAUDE.md`. This file holds the reasoning, the
measured instances, and the wording history behind the Constitution, the hard
rules, the comment policy and the pre-conclusion checklist — read it when a
rule's shape is unclear or when proposing a change to one.

## Constitution

The one-line mission statement is `CLAUDE.md`'s own epigraph — quoted there in
full, not repeated here. Every new feature is read through **eight engineering
lenses** and must stand on the **cross-cutting band**. A lens asks *"does this do X well?"*; the band asks *"does this obey the universals at all?"* — fail a band member and it does not ship.

### The eight lenses — each line is the pass-line (a gate for new work)
1. **System Design** — responsibility sits at the right layer (LLM decides / OS executes / feature owns its domain); no new cross-layer coupling.
2. **Tool Contract** — every side effect rides a typed, validated envelope (Control IR / a typed op), never an untyped string the LLM free-forms.
3. **Retrieval** — the right context is delivered deterministically at the right time (`search_actions` over the tool/mcp/pipeline catalog; the FP-0066 P3b repo-knowledge index (`knowledge_repo_doc`/`knowledge_repo_src`) as a third, OS-internal thing distinct from both — same substrate as `search_actions` but a different, repo-size-proportional population, gated separately via `embedding.index.repo_knowledge` (#4156, default off) so opting into one never silently opts into the other; the FP-0063 user-RAG plugin's bundled ingest/query pipelines for agent-facing document search; the in-core `IndexBackend` substrate is OS-internal, with no agent-callable entry point), not stuffed unconditionally into the prompt.
4. **Reliability** — it recovers from failure (schema-validate + re-prompt, bounded loops with graceful force-close, timeout + opt-in provider-retry); any derived state survives WAL truncation.
5. **Security** — it is permission-gated and sandbox-scoped; no capability reaches the world without passing the gatekeeper.
6. **Evaluation** — its output can be scored against a rubric in-run (an `agent` step + `schema`: the OS constrains generation and validates the parsed result; the threshold comparison is a plain `if`).
7. **Observability** — it leaves an audit-event trace sufficient to inspect and reconstruct what happened (the P6 audit log, `reyn events` replay, live audit chips).
8. **Product Think** — it is predictable, cost-disciplined, and legible to the operator (CLI/CUI affordance, cost reporting, and token-cost *reduction* such as zero-token `present`/offload).

### The cross-cutting band — the foundation every feature obeys
**permission · audit-events · workspace-SSoT · crash-recovery (WAL) · cost/budget (bounding).**

Three lenses name a *discipline* whose *universal mechanism* is a band member: **Security ↔ permission**, **Reliability ↔ crash-recovery (WAL)**, **Observability ↔ audit-events**. The band is where the still-true P5 (workspace) / P6 (events) / P7 (OS-domain-agnostic) survive, demoted from "principles" to the substrate every lens-cell stands on.

*Two honest thin areas (where new work is most valuable): **Retrieval** (the FP-0063 user-RAG plugin's bundled pipelines are the current agent-facing surface — a framework to build on for internal retrieval is `search_actions`/a planned `search_knowledge` verb over the OS-internal substrate, not an agent-callable `semantic_search`) and **Evaluation** (an `agent` step + `schema` is the surviving eval surface — the bespoke `judge_output` scorer op and the eval-export subsystem were both removed; scoring is ordinary agent work riding the OS's typed-schema + cost-tracking substrate, not a special-cased op).*

*"event" is three distinct things — **audit-event** (P6 `.reyn/events`, the audit trail) / **WAL-event** (`.reyn/state/wal.jsonl`, the recovery substrate) / **hook-event** (lifecycle+external reactivity triggers). Never write bare "event".*

*(The full 8×7 populated table lives in `docs/concepts/architecture/charter.md`; this skeleton is the durable core agents read before new work. Tagline: hero = the line above (T1); one-liner/meta = "An agent OS where agency is bounded by construction — decide, spawn, orchestrate, but only through typed, permissioned, auditable, rewindable ops." (T3).)*

### What the gate does not see (2026-08-14, owner-approved)

The eight lenses and the band are a gate on **new features**. Four failures measured in
one day passed straight through, because none of them *was* a feature.

1. **Defaults themselves.** The empty-response `resume` retry was hardcoded on with no
   knob (#4677); the LLM-payload dump that would have explained the incident was
   off (#4501). Neither is a capability — both are a value.
2. **The attitude to an unknown.** `get_max_input_tokens` answers "128,000" for both
   *not loaded yet* and *not in the catalog*, in the same words, warned once per
   process, never corrected (#4680). One value, two states.
3. **The side effect of a repair.** The only practical way to stop the incident was
   `/clear-history`, which unlinks the one file conversation content lives in — so
   asking afterwards what the model had been sent could not be answered (#4501).
4. **What nobody wrote.** #4698 shipped a delta over a baseline held in ONE field on
   a process-shared tracker (`session.py:1176` — "process-shared budget/rate-limit
   tracker"), so two live sessions of the same agent overwrite each other's baseline
   even when every call is `purpose="main"`. It passed the six questions and review:
   the tests construct one series, and no one constructed the configuration that mixes
   two (#4703). The six questions audit the tests that exist; they cannot find the test
   that was never written.

Three questions cover 1–3 — quoted in `CLAUDE.md`, not repeated here — and,
unlike the lenses, apply to changes that add no capability at all: a changed
default, a new fallback, a new recovery command.

**No question is proposed for 4, deliberately.** "What is missing" cannot be
enumerated, and a checklist item asking whether you considered it is answered "yes"
every time — the same shape this section exists to catch. Case 4 was found because
someone asked how a number was computed, and writing the answer forced opening the
call sites. That is a habit, not a gate.

## Hard rules

- **The rule (CLAUDE.md): a mechanism change — code or a doc it mirrors — makes the doc describing it stale, fixed in the same PR.** What's worth keeping here is how wide "describing a mechanism" runs, and why the author alone was never going to be enough to hold it. Wide: a doc goes stale just as easily by describing a *field*, a *call path*, or a *"when wired" claim* that a PR removes or falsifies as by adding a new enum-like variant — and, on the mirror side, by echoing a sibling doc's own wording when the PR's real edit only reaches one of the two copies. #2949→#2958: `control-ir.md:805` kept asserting a recording path through a field that #2958 deleted, surviving because the reviewing pass grepped the doc for the one keyword it expected rather than reading the surrounding prose the PR actually touched. Not enough: an author re-reading their own diff cannot notice the thing their own search never surfaced — a miss reports as silence, not as a flag — so catching it before merge needs a *second*, differently-aimed question, asked by whoever reviews: what does this change make false? Two same-day instances of the mirror form specifically, both caught only after merge, by this session's own doc-drift monitoring, not by review: #4841 fixed CLAUDE.md's TUI colour policy self-contradiction without touching this file's own copy of the same section (#4843); #4851 added a real `interfaces/web/` exemption to the colour-token gate without updating either file's "enumerates every colour-bearing declaration under `interfaces/`" claim (#4853).
- **`docs/reference/runtime/control-ir.md` must stay synced with `OP_KIND_MODEL_MAP`** in `src/reyn/schemas/models.py` (#1983: relocated there from `op_runtime/registry.py` so the `Op` union derives from the same map). New op kinds get a section in the reference in the same PR. **This one is on you, not on CI** — the CI-checked pair is `OP_KIND_MODEL_MAP` ↔ the `Op` union (code ↔ code); nothing opens `control-ir.md` and compares it to anything. (#3410 measured it two ways: of the tests/scripts that name `control-ir.md`, all 8 only quote the convention in a docstring; of the 16 that read `docs/` as real files, none touches `control-ir` or `OP_KIND_MODEL_MAP`. This line previously called it "the sharpest, CI-checked instance" of the doc rule, which read as "a machine is watching" — the exact declared-vs-actual gap the doc rule exists to catch.)
- **The one doc↔code pair CI *does* check** is `docs/reference/runtime/events.md`'s kind enumeration ↔ `AUDIT_EVENT_KINDS` in `src/reyn/core/events/event_schema.py` (#3410) — the audit-event `type` namespace is a **closed vocabulary**, because `.reyn/events` has consumers outside reyn and a kind set that cannot be enumerated is not an interface. Emitting a kind, declaring it, and **enumerating** it is one three-part change; `tests/core/test_audit_event_kind_vocabulary_3410.py` fails on any two without the third, in both directions. **"Enumerating" is the whole of what CI checks** — `_documented_kinds()` reads only the delimited flat list and matches bare identifiers on their own line. `events.md` has a dozen-plus other sections, and **the semantic table row saying what the kind MEANS and which payload fields it carries is on you, not on CI** — the same wording the `control-ir.md` bullet above uses, for the same reason. A kind can be emitted, declared, enumerated, green, and still undocumented in every sense a reader cares about; #4589 landed exactly there, and #4591 then found the five pre-existing `plugin_install_*` kinds (`_started`/`_copied`/`_registered`/`_completed`/`_reconciled`) had been sitting in that state all along, so this is an accumulating gap rather than a one-off. Do not close it by adding a "the table has a row" gate: an empty row satisfies that, which is the make-the-check-the-goal shape this file rejects everywhere else.
- **Recovery-feature PR gate**: any PR adding recovery / reconstruction functionality (WAL-event-derived state, PITR, rewind/restore paths) MUST include a truncate-falsify test verifying the reconstruction source survives WAL truncation below its source events (set X → truncate past X's events → reconstruct → assert X survives). WAL-event-derived recovery state that isn't snapshot-backed is a silent data-loss vector. Same PR, not a follow-up. (Motivated by #2259/#2260.)
- **Sandbox axis-witness gate stays 2-layer**: `enforcement_self_test` (`src/reyn/security/sandbox/self_test.py`) is the PRODUCTION gate every real backend resolution calls; its blast radius is every sandboxed op on every host, so it MUST keep witnessing the deny leg only, for the write + spawn axes only. The richer per-axis 3-tuple contract (deny / exception-boundary / workload — `reyn.security.sandbox.axis_contract`) is CI-conformance-only (`tests/security/test_sandbox_axis_contract_2983.py`, mirroring `scripts/sandbox_landlock_deny_gate.py`'s CI-only deny arms). A PR that folds a new axis or leg into `enforcement_self_test` widens the blast radius of a probe bug to every host's sandbox — do not do this without an explicit owner-level design decision (#2983).

- **TUI colour policy** — the rule itself now lives in `src/reyn/interfaces/CLAUDE.md` (#4840/#4869: relocated out of the root file entirely — "a rule that binds one directory belongs in that directory's own `CLAUDE.md`, not here"), not in this file or the root one; this entry holds only what a directory-scoped rule file can't: the wording history and the gate's own mechanics. **Wording history, in two stages**: an earlier "the terminal emulator's theme decides" opening read as an absolute, contradicting the same section's own reyn-specific/full-colour clause — corrected same-day (#4840/#4841 first pass); this file's own copy briefly drifted out of sync with that fix before being caught (#4843), an instance of the mirror-drift class #4854/#4858 exist to close. Then #4840 landed its real ruling: reyn ships a full-colour default theme now, so the ANSI-16/`ansi_default` escape hatch clause 1 used to offer (`@selection-bg@`/`@selection-fg@` were its own live examples) no longer has a terminal to defer to — owner: "Textual テーマ採用時点で端末の規定色に従う意味は無くなってるでしょ" — and clause 1 was rewritten to a theme-token-or-SGR-only form, ANSI names dropped entirely (#4869). The carve-out (owner, 2026-08-07) had two halves, and only one is superseded: the half that distinguished "conventional meaning → must use ANSI-16" from "reyn-specific meaning → token allowed outside ANSI-16" is moot, because neither branch defers to ANSI any more — but the other half, "a reyn-specific meaning may use any value that serves the design," carries forward unchanged as the new clause 2 itself, and was the standing rationale for #4787's last 2 un-tokenised constants (`_CC_USER_BG`/`_CC_ERR_BG`) — moved into `palette.py` as their own tokens (`@cc-user-bg@`/`@cc-err-bg@`, deliberately not consolidated with `@theme-surface@`'s same-valued but differently-scoped token) by #4934, not new colour values. **Gate mechanics**: `tests/interfaces/test_tui_colour_tokens.py` enumerates every colour-bearing declaration under `interfaces/` — added after two prior greps for an *expected shape* (`$text-muted`, `$var NN%`) each missed a real violation written in a shape nobody searched for (a hex value sitting behind a `border:` keyword). Textual's own `DEFAULT_CSS` is out of scope — reyn doesn't own it (#3525 tracks where it collides with the ansi themes); `interfaces/web/` is out of scope too (#4787②, a browser has no terminal to defer to) — a separate, deliberate exemption on the new Python-hex-literal check, not a gap in the pre-existing CSS-declaration check (which never matched anything under `web/` to begin with; this file's own copy of that fact also briefly drifted, #4851→#4853).

## Comment policy (READ BEFORE WRITING OR MOVING A COMMENT)

The normative source is `docs/deep-dives/contributing/comments.md`, pointed to
from `CLAUDE.md` — this note only draws the one boundary worth stating twice:
what that doc governs (how a comment should read, judged by its content) is a
different axis from what `verification-hazards.md` (linked from CLAUDE.md's
Testing policy section) governs (whether a
green result means what it looks like it means). Confusing the two would send a
comment-wording question to a doc about misread test output, or the reverse.

Key constraints (full rationale in the doc):

- Each test belongs to exactly one Tier (1: Contract / 2: OS invariant /
  3: LLM-replay behavior). Anything that doesn't fit a Tier is **Tier 4 —
  do not write**.
- NEVER fake a collaborator — `unittest.mock.MagicMock` / `AsyncMock` /
  `patch`, or a hand-rolled stand-in class — when a real instance is cheaply
  constructible. Use real instances or the `LLMReplay` Fake. A faked callable
  bypasses signature-drift detection (raises loudly when it should); a faked
  data/state object can silently carry a field the real type doesn't have,
  which raises nothing at all (#3037: an invented `permission_resolver`
  field made a dead permission gate look tested). Same ban, two failure
  modes — see `testing.md` § Mock vs Fake.
- NEVER assert on private state (`tracker._daily_tokens == 100`,
  `mgr._timers["c1"]`, `reg._active[id]`). Use the public surface or a
  `snapshot()`-style read.
- NEVER pin algorithm-level behavior (sort order, dict iteration order,
  internal cache structure, exact whitespace / formatting).
- NEVER add snapshot / golden-file tests outside `tests/scaffold/`.
- Tests carry no time limit of their own — no `@pytest.mark.timeout(N)`,
  no wait-budget constant in the body (`attempts=200`, `range(N)`). Wait on
  the condition unboundedly; a test needing more than CI's `--timeout=120`
  kill switch should be decomposed, not marked. Straight-line `sleep(N)` as
  the thing that makes an assertion pass stays banned. See `testing.md` §
  Time.
- Tests for an extracted refactor belong in `tests/scaffold/` with
  `triggered_by` / `removed_by` metadata, and are **deleted in the PR
  that lands the refactor**.
- Each test docstring's first line must declare its Tier:
  `"""Tier 3a: ..."""`.
- Declaring a Tier presupposes a named behavior/contract that exists
  **outside** the test's own docstring (a doc, a charter lens, a decision
  record, a user-visible promise) — not a new axis, the precondition
  every Tier already carried. See `testing.md` § "The prerequisite every
  tier shares." Distinct from the six questions below: that section asks
  whether an already-tiered test is *well-formed*; this asks whether it
  had standing to claim a tier at all.

## Pre-conclusion observation checklist (READ BEFORE WRITING ANY FINDING / 結論)

**Active trigger**: when you are about to write any of the following — **STOP**
and run the checklist below before continuing:

- 結論 / conclusion / finding / 確定 / decisive
- パターン / pattern / 一貫して / consistently
- 100% / 全件 / N/N / 0% / all / every
- proven / validated / confirmed / 決定的
- attractor / hallucination / regression (= behavioral classification)

**5-question checklist**:

1. List each specific observation that supports the claim — can you?
2. Is each observation **primary data** (= events log / metric / direct
   output) or **inference** from other observations? Inference chains
   downgrade "verified" to "hypothesised".
3. Did you actively look for data that **falsifies** the claim?
4. Is the observation infrastructure (= trace dump / events log /
   metric) actually capturing what you'd need to support the claim?
5. If you're about to write "N/N" or "100%", did you directly inspect
   each of the N items, or did you inspect 1-2 and extrapolate?

**Re-frame instead of overstating**:

- ❌ "X happens 100% in condition Y" (= when only 1-2 of N inspected)
- ✅ "Hypothesis: X may dominate in Y. Direct verification: 1/N. Remaining
  N-1 inspection pending."

**Reference**:
- `feedback_pre_conclusion_observation_checklist.md` (full 5-question detail
  + failure-mode patterns)
- `feedback_observe_before_speculate_llm.md` (passive principle this trigger
  operationalises)
