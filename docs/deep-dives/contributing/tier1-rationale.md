# Tier-1 rules — full rationale

Normative form lives in `CLAUDE.md`. This file holds the reasoning, the
measured instances, and the wording history behind the Constitution, the hard
rules, the comment policy and the pre-conclusion checklist — read it when a
rule's shape is unclear or when proposing a change to one.

## Constitution

> **Reyn is an operating system for LLM agents** — they decide, organize, and orchestrate; the OS makes every action typed, permissioned, audited, and recoverable by construction.

Every new feature is read through **eight engineering lenses** and must stand on the **cross-cutting band**. A lens asks *"does this do X well?"*; the band asks *"does this obey the universals at all?"* — fail a band member and it does not ship.

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

Three questions cover 1–3, and unlike the lenses they apply to changes that add no
capability at all — a changed default, a new fallback, a new recovery command:

- **Who stops this if it repeats?** Name the bounding subject, or there isn't one.
- **Is this visible with the shipped config?** If seeing it requires changing a
  setting, it is not visible.
- **Does the repair destroy the evidence?** If it does, say what survives it.

**No question is proposed for 4, deliberately.** "What is missing" cannot be
enumerated, and a checklist item asking whether you considered it is answered "yes"
every time — the same shape this section exists to catch. Case 4 was found because
someone asked how a number was computed, and writing the answer forced opening the
call sites. That is a habit, not a gate.

## Hard rules

- **A doc describing a mechanism is stale the moment that mechanism's code changes — fix the doc in the SAME PR, not a follow-up.** This is broader than adding a new enum-like variant: a doc goes stale just as easily by describing a *field*, a *call path*, or a *"when wired" claim* that the PR removes or falsifies. (#2949→#2958: `control-ir.md:805` kept asserting a recording path through a field that #2958 deleted — survived because the reviewing pass grepped the doc for the one keyword it expected, not the surrounding prose describing what the PR touched.) When a PR changes something a doc describes, re-read the whole section the change touches, not just the line whose keyword you already had in mind.
- **`docs/reference/runtime/control-ir.md` must stay synced with `OP_KIND_MODEL_MAP`** in `src/reyn/schemas/models.py` (#1983: relocated there from `op_runtime/registry.py` so the `Op` union derives from the same map). New op kinds get a section in the reference in the same PR. **This one is on you, not on CI** — the CI-checked pair is `OP_KIND_MODEL_MAP` ↔ the `Op` union (code ↔ code); nothing opens `control-ir.md` and compares it to anything. (#3410 measured it two ways: of the tests/scripts that name `control-ir.md`, all 8 only quote the convention in a docstring; of the 16 that read `docs/` as real files, none touches `control-ir` or `OP_KIND_MODEL_MAP`. This line previously called it "the sharpest, CI-checked instance" of the doc rule, which read as "a machine is watching" — the exact declared-vs-actual gap the doc rule exists to catch.)
- **The one doc↔code pair CI *does* check** is `docs/reference/runtime/events.md`'s kind enumeration ↔ `AUDIT_EVENT_KINDS` in `src/reyn/core/events/event_schema.py` (#3410) — the audit-event `type` namespace is a **closed vocabulary**, because `.reyn/events` has consumers outside reyn and a kind set that cannot be enumerated is not an interface. Emitting a kind, declaring it, and **enumerating** it is one three-part change; `tests/core/test_audit_event_kind_vocabulary_3410.py` fails on any two without the third, in both directions. **"Enumerating" is the whole of what CI checks** — `_documented_kinds()` reads only the delimited flat list and matches bare identifiers on their own line. `events.md` has a dozen-plus other sections, and **the semantic table row saying what the kind MEANS and which payload fields it carries is on you, not on CI** — the same wording the `control-ir.md` bullet above uses, for the same reason. A kind can be emitted, declared, enumerated, green, and still undocumented in every sense a reader cares about; #4589 landed exactly there, and #4591 then found the five pre-existing `plugin_install_*` kinds (`_started`/`_copied`/`_registered`/`_completed`/`_reconciled`) had been sitting in that state all along, so this is an accumulating gap rather than a one-off. Do not close it by adding a "the table has a row" gate: an empty row satisfies that, which is the make-the-check-the-goal shape this file rejects everywhere else.
- **Recovery-feature PR gate**: any PR adding recovery / reconstruction functionality (WAL-event-derived state, PITR, rewind/restore paths) MUST include a truncate-falsify test verifying the reconstruction source survives WAL truncation below its source events (set X → truncate past X's events → reconstruct → assert X survives). WAL-event-derived recovery state that isn't snapshot-backed is a silent data-loss vector. Same PR, not a follow-up. (Motivated by #2259/#2260.)
- **Sandbox axis-witness gate stays 2-layer**: `enforcement_self_test` (`src/reyn/security/sandbox/self_test.py`) is the PRODUCTION gate every real backend resolution calls; its blast radius is every sandboxed op on every host, so it MUST keep witnessing the deny leg only, for the write + spawn axes only. The richer per-axis 3-tuple contract (deny / exception-boundary / workload — `reyn.security.sandbox.axis_contract`) is CI-conformance-only (`tests/security/test_sandbox_axis_contract_2983.py`, mirroring `scripts/sandbox_landlock_deny_gate.py`'s CI-only deny arms). A PR that folds a new axis or leg into `enforcement_self_test` widens the blast radius of a probe bug to every host's sandbox — do not do this without an explicit owner-level design decision (#2983).

- **TUI colour policy: the terminal emulator's theme decides, reyn only names WHICH semantic colour, never WHICH RGB.** Owner ruling (#3525, 2026-07-31): "the terminal emulator's theme should take priority over Textual's own theme." Normative form **for a meaning the terminal's theme already has an opinion about** (see the scope test below — this is not every colour in the TUI): use ANSI 16 (`ansi_red` etc. — the terminal resolves it), `ansi_default`, `transparent` (paint nothing), or a `text-style` attribute (`dim`/`bold`/`reverse`) — never a hex literal in a stylesheet, and never alpha-composite over `ansi_default` (the blend loses the terminal's own value and becomes hex-equivalent, #3505's `#0c0c0c` residue). Every colour reyn's inline CUI uses lives in one file, `src/reyn/interfaces/inline/textual_chat/palette.py`; every widget stylesheet writes a `@name@` marker instead of a literal value, and `tests/interfaces/test_tui_colour_tokens.py` enumerates every colour-bearing declaration under `interfaces/` and fails on any value named outside the palette — added after two prior greps for an *expected shape* (`$text-muted`, `$var NN%`) each missed a real violation written in a shape nobody searched for (a hex value sitting behind a `border:` keyword). Textual's own `DEFAULT_CSS` is out of scope for this gate — reyn doesn't own it (#3525 tracks where it collides with the ansi themes).

  **Scope — decide this before choosing any value:**

  1. Does the meaning have a convention a reader already carries (*error*,
     *success*, *in-flight*)? → name the meaning, let the theme render it
     (ANSI-16 / `ansi_default` / SGR attribute).
  2. Is the meaning reyn-specific? → any value that serves the design,
     full-colour included. The theme has no opinion to defer to here.

  **A colour is not a meaning.** "Error" is the meaning; red is one
  terminal's rendering of it. Never pick the colour first.

  **This is not an ANSI-16-only policy.** ANSI-16 is how reyn defers on (1);
  it is not a palette reyn is confined to. Only two things are forbidden, and
  neither limits expressiveness: a literal in a widget stylesheet (values go
  through a `palette.py` token), and alpha-compositing over `ansi_default`
  (the blend destroys the terminal's own value).

  **Carve-out (owner, 2026-08-07, extends #3525 rather than replacing it):** a
  *reyn-specific* meaning — one with no established convention the way
  *error* or *success* has one — may use a token value outside ANSI-16. A meaning WITH an
  established convention still must resolve through ANSI-16 /
  `ansi_default` / an SGR attribute, same as before; "is this meaning
  conventional or reyn-specific" is a judgment call no gate can make, which
  is exactly why the token-indirection requirement is not optional here —
  **every value, conventional or reyn-specific, still goes through a
  `palette.py` token, never a literal in a stylesheet.** Only the token
  layer is where the carve-out applies; without that constraint, a
  stylesheet could claim "reyn-specific" for anything and the value gate
  would have nothing left to check. The semi-transparent-over-`ansi_default`
  ban above is unchanged by this carve-out.

## Comment policy (READ BEFORE WRITING OR MOVING A COMMENT)

The comment policy is at **`docs/deep-dives/contributing/comments.md`** — normative,
read it before deleting, compressing, or relocating a code comment. It classifies
comments by content (never by length), gives the one-question test for the class
that must stay inline regardless of size, and states why a residue should read
"X breaks" rather than "do not change this." This is a code-authoring policy, not
a verification one — do not conflate it with `verification-hazards.md` above,
which is about misreading a green test/gate result, a different axis entirely.

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
