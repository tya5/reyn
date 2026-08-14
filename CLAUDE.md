# CLAUDE.md — Reyn Agent OS rules

Tier 1 hard rules for code-writing agents. Read on demand for rationale and
deep dives via the references at the bottom.

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

- **TUI colour policy: the terminal emulator's theme decides, reyn only names WHICH semantic colour, never WHICH RGB.** Owner ruling (#3525, 2026-07-31): "the terminal emulator's theme should take priority over Textual's own theme." Normative form: use ANSI 16 (`ansi_red` etc. — the terminal resolves it), `ansi_default`, `transparent` (paint nothing), or a `text-style` attribute (`dim`/`bold`/`reverse`) — never a hex literal, and never alpha-composite over `ansi_default` (the blend loses the terminal's own value and becomes hex-equivalent, #3505's `#0c0c0c` residue). Every colour reyn's inline CUI uses lives in one file, `src/reyn/interfaces/inline/textual_chat/palette.py`; every widget stylesheet writes a `@name@` marker instead of a literal value, and `tests/interfaces/test_tui_colour_tokens.py` enumerates every colour-bearing declaration under `interfaces/` and fails on any value named outside the palette — added after two prior greps for an *expected shape* (`$text-muted`, `$var NN%`) each missed a real violation written in a shape nobody searched for (a hex value sitting behind a `border:` keyword). Textual's own `DEFAULT_CSS` is out of scope for this gate — reyn doesn't own it (#3525 tracks where it collides with the ansi themes).

  **Carve-out (owner, 2026-08-07, extends #3525 rather than replacing it):** a
  *reyn-specific* meaning — one with no established convention (red = error,
  etc.) — may use a token value outside ANSI-16. A meaning WITH an
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

## Testing policy (READ BEFORE WRITING TESTS)

The testing policy is at **`docs/deep-dives/contributing/testing.ja.md`** (English:
`docs/deep-dives/contributing/testing.md`). It is normative — read it before adding
or modifying tests. For co-vet review and gate-design specifically, also read
`docs/deep-dives/contributing/verification-hazards.md` — a checklist built on one
root, stated in the doc's own words: **an observation does not name its own
referent**. Most instances are a green that means less than it looks like, but
the doc covers the other direction too (a red that overstates — a misattributed
failure count, an absence that was really a stale tree), so do not read it as
green-only. Each entry carries a real instance and a detection technique that
closed it.

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

## Test review — six questions, asked of the test's own code

**Read the tests in the diff, not the PR body's account of them.** `test_tier_audit.py`
reads the code for five of its six checks; the Tier line it matches as a *string*
(`^Tier [123][abc]?:`). A declaration is not a classification, and nothing else
looks. On 2026-08-09 two tests pinning TerminalTextEffects' own behaviour passed
that audit, passed review, and one of them cost the operator three reboots.

Ask each test in the diff:

1. **Which Tier does it fit — 1, 2, 3, or none?** Name the one, not the word
   "Tier". `testing.md` is a whitelist: what fits no Tier is Tier 4 and is not
   written. Two shapes that look like they fit and do not — **a third party's
   property** (#3872: "a TTE effect resolves to its input" is TTE's promise, not
   reyn's) and **a past bug's fingerprint** (`assert "reyn" not in final`, which
   any *other* wrong string passes). Answering "it's reyn's" is not an answer:
   reyn's own trivia fits no Tier either. "Third party's property" is a
   discriminator, not a TTE-only example — it recurred unrecognized in the
   sandbox suite (kernel-level SBPL/Landlock deny enforcement) precisely
   because it had only ever been written down as one case. The general
   form: *if this assert fails, whose bug is it?* — kernel/library code
   fails it, reyn's own code doesn't. See `testing.md` § "Third-party
   promises are not reyn's to test" for the full discriminator and the
   twin-test tell that flags most kernel-level cases.
2. **Is it the implementation, transcribed?** If the same expression appears on
   both sides, it can only fail when someone deliberately edits that line — and
   they will edit both. (#3872: `art = "\n".join(covered)` asserted back.)
3. **Who would miss this test if it were gone?** Not whether the assert
   currently fires — whether execution can tell you that for free, and
   review should not re-derive what a CI run already answers. Three
   answers: *nobody* → delete. *A situation only this test itself
   constructs* (production never builds that configuration) → delete.
   *A production consumer, or another real mechanism* → keep. The middle
   answer is the discriminator, and it catches more than one shape: a
   hand-written stub that subclasses the production class and breaks one
   branch (`#3902`'s `_NoFoldEventLog`, `#3916`'s
   `_PreNineOhOneAgentLayer` — see `testing.md` § "The strip-falsify
   mimicry"), and — same answer, different shape — a manually assembled
   collaborator list with one layer removed by hand
   (`#3916`'s `test_falsification_removing_a_layer_regrants_a_denied_capability`:
   `EffectivePermission([AgentLayer(decl)])`, a combination production
   never constructs). Both fail this question on the same line, because
   both are configurations only the test itself builds, not something a
   real caller ever hands it. "Was handed X" is not a witness for "used X"
   (#3859), and #3850 landed a field that was required, populated, tested,
   and read by nobody — the honest answer to "who would miss it" was
   already "nobody."
4. **Would it stay green having never run?** skip / collection error / zero
   collected all wear green's colour. Name what a missing optional dependency
   silently skips — CI has no `effects` extra, so #3796's file skips whole and
   its green says nothing (#2999 is the same shape with a docker-daemon skip).
5. **What does it accumulate, and who bounds it?** A `list()` over a producer
   whose length is decided by the *caller's* pace is unbounded by construction.
   (#3872: the app's timer paced it at 10fps; `list()` paced it at CPU speed, and
   the collecting starved the worker thread it was waiting on — 10 GB.)
6. **Is the declared Tier the true one?** Only a human can answer this; the audit
   cannot. Say which of 1's answers you reached and why.

**Which answers block.** The questions produced the right observation on their
first use and the PR merged anyway: #3876's ⑤ answer was "bounded only by the
thread scheduler, not by the test" — written down, measured at 413 MB, and let
through as a note. The operator had to ask why. **An answer recorded is not an
answer acted on**, which is the same gap as an audit that reads a Tier string.
So each question has a blocking answer, not just an answer:

| | blocks when the answer is |
|---|---|
| 1 | **none** — including a third party's, a past bug's, and reyn's own trivia |
| 2 | yes — the same expression is on both sides |
| 3 | **nobody**, or only a configuration this test itself constructed |
| 4 | it would be green having never run, **and the PR does not say so** |
| 5 | **anything outside the test bounds it** — a thread, a timer, the caller's pace |
| 6 | the declared Tier is not the one question 1 named |

⚠️ 4 blocks on the *silence*, not on the skip: a file that skips whole in CI is
often correct (an optional extra), and what makes it a defect is a green nobody
qualified. 5 has no such carve-out — "it is small today" is a measurement of
today, and the runaway that started this was small until it wasn't.

**3 needs no accept-side exception.** An accept-side test ("this shape must
NOT trip the gate") is not a special case of question 3 — it has its own
job, catching over-firing, and its consumer is the gate's own users, who
would be wrongly blocked without it. Asked "who would miss this test," an
accept-side test answers the same way any other real test does: the
operators the gate would have false-positived against. No carve-out is
needed because question 3 was never the wrong question for it — question 3
in its earlier phrasing ("would it stay green with the mechanism dead?")
was the wrong question for it, since an accept-side test is *supposed* to
stay green with the gate's deny-firing mechanism removed. Asking who'd miss
it instead of whether it's green resolves this without a special case.

**Reviewer's note, on the PR:** record the answers per test before merging. A
lead-coder merge train refuses any PR touching `tests/` without one — the promise
"I will open the tests next time" is exactly the shape this replaces.

**When something forces you to touch a test — a dependency bump, a rebase, a CI
failure — ask "should this exist" before "how do I make it pass again."** A
flowview pin bump (0.16.0 → 0.16.1, #3886) broke one test's premise ("a fresh
session is a blank screen" — it never quite was; reyn's own welcome placeholder
was always painted, just invisible to the older, narrower capture). The first
pass **repaired** it: split into a blank-canvas guard test and a positive
welcome-text test, both green, six questions answered, gates clean. Wrong move —
caught only because the operator asked "私ならそんなテスト捨てるけどね" (I'd just
delete that test) after seeing the diff, not because anything in the checklist
stopped it. Re-applying the six questions with delete as the live option, not
repair:

- The guard test was redundant — falsifying the guard it protects still didn't
  crash, because `test_every_attempt_failing_hands_back_a_held_legible_screen`
  already covers "every attempt fails" generally, blank input included.
- The welcome-text test pinned a THIRD PARTY's property under reyn's name:
  `text_effect.py` does nothing with `covered`'s content, so whether the
  welcome placeholder shows up in `covered` at all is flowview's capture
  behaviour, not reyn's — Q1's "third party" carve-out, missed because the
  code on both sides of the assertion was reyn's own call site, not reyn's own
  logic.

Both deleted. **The failure mode was ORDER, not the checklist's content**: the
same six questions were applied minutes earlier to the same PR and caught real
things (#3876's review) — but applied in "does this still pass" order, which
starts from the code that exists and looks for a way to keep it. Starting from
"should this test exist at all" is a different search, and repair-mode never
runs it. A rebase/bump forcing a touch is exactly the moment deletion is
cheapest — the test is already broken, and "make it green" is not the only
available action.

## PR workflow (READ BEFORE OPENING / REVIEWING A PR)

This repo is touched by multiple Claude sessions (lead-coder, e2e-coder,
per-PR coders) authenticating as the same `gh` user.

**Issue-triage label: `blocked:external`** (owner-introduced, 2026-08-14).
An open issue carrying it needs owner judgment or an upstream
third-party dependency before an OS-side session can move it forward
alone — an open issue WITHOUT it is pickable by any peer session.
Measured containment (#4549's own methodology, re-applied to all four
labels): `owner:decide` / `needs:owner-decision` / `owner:verify-only` /
`wait_owner_iv` are each entirely a SUBSET of `blocked:external`, but
not the reverse — `blocked:external` also covers external blockers
with none of those four labels (a pure third-party wait, no
owner-decision axis at all — verified narrower the same night this
paragraph was written: a first-pass reading of the gap between the
four-label union and `blocked:external` mistook 3 genuinely
owner-pending issues for third-party waits, because they were missing
an owner-axis label, not because the containment claim itself was
wrong). This paragraph is the label's only durable record; it existed
only as broker chat before this.

**Before you open a PR, run `ruff check .`, `python
scripts/test_tier_audit.py --strict <changed test files>`, `python
scripts/verify_module_docstrings.py <changed src files>`, `python
scripts/mypy_ratchet.py`, `python scripts/flat_tests_ratchet.py`
(#3879 Stage 0 — CI runs this unconditionally on every PR, no path
filter, `.github/workflows/flat-tests-ratchet.yml`; it only fails when
a NEW `.py` file lands directly in `tests/`, not in any subdirectory —
cheap enough to just always run), `python
scripts/check_tests_path_literal_reference.py` (#4065/#4068 — a `tests/...py`
path-literal ratchet, ~2.6s; on a PR that moves/renames a `tests/...py` file,
its whole-repo baseline goes stale the moment `main` moves underneath your
branch, so re-run it right before pushing, not earlier in the session —
#4068 rebased 4 times over this before the cause was identified, #3880),
and `pytest` scoped to the files/keywords your
diff actually affects.** **Do NOT run the full `pytest` suite from the repo
root locally** — CI already does that, in a clean checkout, faster and
without picking up local machine config a full local run would (#3791).
This is testing.md's own canonical policy, not a CLAUDE.md-specific rule —
read `docs/deep-dives/contributing/testing.md` § "Before you push" for the
full reasoning (why the full run was dropped, not just discouraged; what
running it locally actually costs vs. CI; the #3750 count history) before
changing this paragraph, in either doc. This line said `ruff check src tests` until #4630 measured the gap: CI runs `ruff check .` (`test.yml:162`), so the documented command silently skipped `scripts/` and every other top-level directory — a PR author who followed it exactly still went red, and 17 genuinely-dead imports outside `src/` had been invisible to the whole checklist. Run the same command CI runs; a narrower local gate is a green that does not mean what it says.

**If your PR touches `docs/`, also run `mkdocs build --strict -f
.mkdocs/mkdocs.yml && python scripts/check_doc_anchors.py && python
scripts/check_retired_config_keys_denylist.py`** — as a sequence, in
that order (the first two are a pair, not either alone; the third is
independent of the first two but lives in the SAME CI job so belongs
in the same local run). CI's "docs build (strict)" job runs all three
steps (`test.yml`'s `docs` job — #4651 caught #4660's own fix stopping
at 2 of the job's 3 steps, the same under-scoping class this whole
paragraph exists to close); `mkdocs build --strict`
catches a dangling *file* reference but never checks whether `#anchor`
actually exists on the target page, which is `check_doc_anchors.py`'s
own job, run against the `site/` the mkdocs step just built (#3557/
#3592: 42/42 line-number citations in `charter.md` had drifted,
silently, before this script existed); `check_retired_config_keys_denylist.py`
(#4327) is a separate, unrelated check in the same job — a retired
top-level `reyn.yaml` key (renamed via #4174) must not appear, at
YAML top level, in operator-facing docs or `reyn.local.yaml.example`.
Running `check_doc_anchors.py`
alone without a prior `mkdocs build` first raises an `AssertionError`
from a missing `site/` — which reads as "main is broken," not as "run
the other command first" (#4651: this exact confusion, the same
`git grep` finding 0 mentions of the script in either this file or
`testing.md`, until now).

**If your PR touches `src/reyn/mcp/`, also run `python
scripts/check_fastmcp_import_boundary.py`** (#3698 enforcement half) —
a dedicated, path-filtered CI workflow
(`.github/workflows/fastmcp-import-boundary-gate.yml`, triggered on
`src/reyn/mcp/**`, not part of `test.yml`), so it never runs on a PR
that doesn't touch this directory. Zero-baseline: no file under
`src/reyn/mcp/` may `import fastmcp` at all, since the last file that
needed to (`_fastmcp_boundary.py`) was retired clean-break (#4302).

**If your PR touches `tests/`, also run `python
scripts/check_bare_tests_import_reference.py` and `python
scripts/check_file_depth_reference.py`** (#4008 / #3995-#4002-#4019) —
two more dedicated, path-filtered workflows (both triggered on
`tests/**`, both `tests_dir.rglob("*.py")` whole-tree scans against a
baseline of zero, neither part of `test.yml`). The first rejects a
bare `from _some_module import x` (no `tests.` prefix) in a test file
— it resolves today only because pytest's "prepend" import mode
happens to put a flat consumer's own directory on `sys.path`, and
silently breaks the moment that consumer moves into a subdirectory.
The second rejects a module-level `.glob(...)`/`.rglob(...)` call
whose root, resolved from the file's OWN current location, escapes
`tests/` or lands outside its current direct child directories — a
static, add-time proxy for "this path expression won't survive a
future move," not a check that anything has actually moved yet.

A green scoped `pytest` alone is
**not** a green CI run (`pytest-green ≠ CI-green`): ruff `I001` import-sort
and a Tier-4 format pin (`len(...) == N`) both fail CI while `pytest`
passes.

These rules then keep multi-session work coherent:

1. **Finish your own Test plan before merge.** PR authors run every
   Manual / Visual item in the Test plan and tick the box, or replace
   `- [ ]` with `- [x] (skipped — <reason>)`. Reviewers do not merge
   while items are unchecked without an explicit waiver.
   **Standing waiver, Visual items only (owner, 2026-08-08)**, quoted
   verbatim on one line so the boundary of the owner's own words is
   unambiguous: 「あと見た目ゲートだとしてもmainマージ進めてよ。そうしないと会社で見れないんだから」
   (roughly: proceed with the main merge even for visual gates —
   otherwise I can't see it at the company). The operator's only way to
   actually SEE a visual/TTY result is on `main` (their own execution
   environment), so waiting for a visual check before merging inverts
   the real dependency: the check needs the merge, not the other way
   around. Leave the Visual box
   unchecked in the Test plan (do not fabricate a check that didn't
   happen) and merge anyway; the operator confirms after, on `main`,
   and a follow-up PR fixes anything they flag. **This loosens the
   Visual axis ONLY.** Falsification, consumer-sweep, and CI gates are
   unaffected and still block merge exactly as before — this is not a
   general "a waiver exists somewhere, so merge unchecked" license.
2. **Role-prefix every issue / PR body / PR comment.** Start the PR
   body AND each follow-up comment with `**[role-name]** — ` (e.g.
   `[lead-coder]`, `[e2e-coder]`, `[tui-coder]`, `[dogfood-coder]`,
   `[per-PR-coder]`, `[security-reviewer]`) so the recipient session
   can tell "this is feedback for me" from "I wrote this earlier
   myself". **The PR body counts** — it is the first comment a
   reviewing session reads, and without the prefix the role of the
   author can only be inferred from branch naming (= a hint, not the
   workflow contract). The `Co-Authored-By: Claude` commit trailer
   does not propagate to PR comments, so this prefix is the only
   cross-session signal. **This isn't just a courtesy convention — it's
   the ONLY signal, because every session authenticates as the same
   `gh` user.** `gh pr view N --json author` returns the same account
   (`tya5`) regardless of which session opened it; it cannot
   distinguish sessions the way it would across different human
   accounts, so recommending it as a way to identify a PR's author role
   is a real trap, not a redundant-but-harmless check (a lead-coder
   session did exactly this before catching it: `--json author` on a
   `tui-coder`-authored PR returned `tya5`, same as every other PR,
   and only the body's own `**[tui-coder]**` prefix actually said who
   wrote it).
3. **If broker MCP is connected, supplement PR comments with
   `post_message` for time-sensitive coordination.** When a session
   would otherwise wait for the peer's next manual polling to notice
   a block or revision-ready signal, send a parallel broker message
   (= `post_message(to=<peer>, ...)` with a short summary). Typical
   uses: "revision pushed, ready for re-review", "block raised on
   #N", "I'm picking up #M, please pause on it". **PR comments
   remain the authoritative audit trail** — review decisions (block /
   accept / merge), revision rationale, and review evidence all stay
   in PR body / comments / commit messages. Broker is only for
   reducing reviewer-side latency. When broker is unavailable or the
   peer is offline, fall back to PR comment alone — the workflow
   still works.

   **Broker semantic limits (= treat broker as best-effort hint):**
   - **In-memory only**: broker process restart drops all queued
     messages. If the broker maintainer announces a restart, every
     session must rewrite any in-flight coordination signal as a PR
     comment so the contract is preserved.
   - **Up to ~30s lag**: watcher polls at ~30s intervals, so a
     "block raised on #N" race with the peer's `git push` is not
     fully closed by broker alone. Truly critical pause / block /
     "do not merge" signals must land on the PR comment **and** via
     broker — never broker alone.
   - **No ack semantics**: `post_message` returns `queued` only.
     The sender cannot tell whether the recipient has read or acted
     on the message. For coordination that must be confirmed, ask
     the peer to ack via broker reply, or verify the outcome on the
     PR (= comment posted, push paused, etc.). Do not assume "I
     posted, therefore they paused".

   In one line: **broker = hint, PR = contract**.

4. **Closing-keyword caution (sub-PR arcs).** GitHub `closes/fixes/resolves #N`
   keywords match **literally** regardless of sentence context — a sub-PR body
   containing `closes #X` auto-closes `#X` on merge even if the sentence reads
   "this PR partially closes #X". For sub-PRs in a multi-PR arc, use `part of
   #X` or `toward #X`. Only the final PR that actually completes the umbrella
   issue should use `Closes #X`.
   **This includes a sentence explicitly saying the PR does NOT close #X** —
   "Does not close #X" and "closing #X is a separate call" both auto-close
   #X on merge, because GitHub's parser matches the keyword-plus-number
   pair with no awareness of negation or of the sentence being ABOUT the
   decision rather than making it. Careless writers rarely hit this — `part
   of #X` alone says everything needed. It's specifically hit by writers
   trying to explain, carefully, why they're deliberately NOT closing
   something (#3808, #3809, same night) — the more the sentence tries to
   spell out the non-closing intent, the more likely it is to place the
   keyword right next to the number. If you want to say a PR doesn't close
   #X, say `part of #X` (or nothing) and leave "close" out of the sentence
   entirely — don't write a sentence that contains both the keyword and the
   number, no matter which way the sentence is negated.
   **Determining "final" is not a guess**: before writing `Closes #X` into a
   brief or PR body, enumerate every PR/issue whose body contains `part of
   #X` (`gh pr list --state all --search "#X in:body"`) and confirm none are
   still open. "Probably the last one" is not sufficient — a close closes
   the umbrella issue's visibility along with it, so a wrong guess hides the
   remaining work rather than merely failing to finish it (#3368: a `Closes`
   fired 40 minutes before the arc's actual last PR merged, because the
   enumeration step was skipped).
   **Record the enumeration in your Test plan, in plain prose** — quoting
   `part of #X` there is safe, anywhere in the body. When the body also
   declares `Closes #X` unfenced, `scripts/check_pr_closing_intent.py` reads
   any other `part of #X` in it as a mention rather than a scope declaration,
   so the record needs no marker, no fence, and no particular ordering
   (#3559 — before that fix, recording it is what turned a rule-4-compliant
   PR red, penalising the discipline the gate enforces).
   **Reviewer recovery angle:** an unexpected issue auto-close triggered by a
   sub-PR merge is almost always a closing-keyword false positive. Reopen the
   issue and verify the arc is not half-done before assuming completion.
   **This rule's checks all read the PR body — a `git commit` message is a
   SEPARATE surface the same keyword-plus-number matching applies to, and
   fixing it costs more.** `scripts/check_pr_closing_intent.py` scans every
   individual commit message in the PR too (not just the body), because a
   squash-merge's default commit message is the *concatenation* of the PR's
   own commit messages — a closing keyword sitting in any one of them
   appears in that default squash body regardless of what the PR body says
   (#3187: the PR body was clean; an intermediate commit's message wasn't;
   the squash-merge auto-closed the wrong issue). Editing the PR body does
   NOT fix a commit-message violation — the gate stays red until the
   offending commit is `amend`ed (or the history is rebased) and
   force-pushed, real extra cost a body edit never needs (#4443 hit this
   directly). The avoidance is the same single rule as the body's own: never
   place a closing keyword next to an issue number, in either surface.
5. **No blanket en/ja mirror obligation.** JA docs are a curated, intentionally
   partial subset (614 EN files vs 125 JA, repo-wide) — a PR is not required to
   touch a `.ja.md` file just because an EN sibling exists or just changed. Do
   fix a `.ja.md` file's own claims if the PR falsifies something it currently
   asserts (same reasoning as any other doc-drift fix, not a mirror rule); do
   not invent a "keep en/ja pairs in sync" obligation when scoping a PR. Known
   ja-parity gaps are tracked in #2967 as backlog, not a per-PR gate.
6. **Arc-closure remainder rule.** When closing an arc, settle every remainder
   as either **filed** or **explicitly dropped** — **in the closing comment**.
   Never leave "next arc" as the resting state: that is a third, silent state
   (a decayed intent, not a decision), evidenced by #2597's natural experiment
   — one remainder recorded as "server role deferred" survived because it was
   named in the closing comment; an informally-mentioned "spec gap analysis"
   remainder existed nowhere (not the issue, not docs, not the proposing
   session's own memory) and rotted invisibly. A remainder belongs on the
   surface the merge gate actually reads (an open issue, an unchecked Test
   plan item) — not on a surface nobody re-checks (a broker message, a memory
   pin, a comment's prose that never becomes a ticket).
7. **A reviewer's blocking point goes in the PR body as an unchecked Test
   plan item, not only in a review comment.** `gh pr review --request-changes`
   does not work in this repo's setup: every session authenticates as the
   same `gh` user, and GitHub refuses to let an account request changes on
   its own pull request — there is no machine-readable `CHANGES_REQUESTED`
   state available here. A review comment alone is easy to miss because
   nothing about the PR's own checks-passing state reflects it: "all checks
   green" can be — and four times in one day was (#3720 ×2, #3722, #3730) —
   reported while a still-open review comment sat unaddressed, because the
   comment lived on a surface the merge decision doesn't read. This is
   **the reviewer's obligation**, not the implementer's: rule 1 above
   already covers the implementer side (finish your OWN Test plan before
   merge). A reviewing session that has a blocking point edits the PR body
   to add it as `- [ ] 🔴 <point>` (append, don't remove the author's own
   items) and does not merge while it's unchecked; the author checks it off
   once addressed, in the same PR body, with the fixing commit noted.
   **Rule 4 applies to this edit too** — a reviewer's appended text lands
   on the same PR body surface rule 4 governs, and `check_pr_closing_intent.py`
   deliberately strips backticks before matching (the criterion is *use vs
   mention*, not "is it fenced"), so pointing out that a PR wrongly wrote
   `Closes #X` by quoting `` `Closes #X` `` in your own blocking item still
   trips the gate — the correct PR being blocked over how the correction
   was phrased (#3559's shape again: the disciplined party pays). Point at
   the problem without writing the keyword next to the number — name which
   line has it wrong, or say "uses the closing keyword" without the keyword
   itself; a literal `<!-- closing-check: discussing #N -->` comment is the
   last resort when the keyword must appear verbatim. **This applies when
   handing someone else phrasing to use, too, not only when writing your
   own PR body** — a brief, a dispatch message, or a review request that
   pairs the keyword with a number gets copied faithfully by whoever
   receives it, and the trap fires on THEIR PR, not the sender's. Check
   phrasing you're about to hand off the same way you'd check your own.
8. **A PR touching `tests/` does not self-merge until a reviewer's
   TESTS-READ note lands on the PR.** "A lead-coder merge train refuses
   any PR touching `tests/` without one" (Test review, above) describes
   what *that train's own script* does — it is not a rule that reaches a
   session merging directly. `#3916` (75 files, 31 under `tests/`) merged
   with no TESTS-READ ever posted: not blocked, not late — the gate
   the sentence implied was never wired to that path at all, the same
   declared-vs-actual gap this file has been naming all session, this
   time in its own PR-workflow section. The content was sound (confirmed
   post-hoc) and the author was not at fault — "resume" was said without
   the caveat that merge still waits on TESTS-READ, an omission on the
   reviewer's side, not a violation on the implementer's. No CI gate: a
   check for "a comment containing a fixed string exists" makes passing
   the check the goal (an empty TESTS-READ satisfies it) — the same shape
   already closed elsewhere in this file. A doc line is the proportionate
   fix; only a human reading the PR before merging can tell a real
   TESTS-READ from an empty one.

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

## When in doubt — read these

- **Verification hazards** (why an observation may not name its own referent —
  a green that means less, or a red that overstates; co-vet review, gate
  design): `docs/deep-dives/contributing/verification-hazards.md`
- **Workspace** (single source of truth): `docs/concepts/runtime/workspace.md`
- **Events / replay** (audit truth): `docs/concepts/runtime/events.md`
- **`.reyn/` directory layout** (what's recovery-core vs persist/audit/cache/outside, the
  recovery-core write-gate, where new subsystem data goes):
  `docs/reference/runtime/reyn-dir-layout.md`
- **Permission model**: `docs/concepts/runtime/permission-model.md`
- **Op catalog and dispatch**: `src/reyn/core/op_runtime/`
- **Tool naming convention** (word-order, removal/fetch-one/install verb classes, drift-gate rationale): `docs/reference/runtime/tool-naming.md` (#3223)
- **LLM trace analysis**: `docs/reference/dogfood-tracing.md` — `scripts/dogfood_trace.py --mode llm-payloads` is the canonical entry point for inspecting LLM payloads; do not hand-parse JSONL.
- **Full feature inventory**: `docs/feature-map.md` — every implemented feature grouped by subsystem, each linked to its reference/concept doc (impl-extracted; impl↔doc mirror).
