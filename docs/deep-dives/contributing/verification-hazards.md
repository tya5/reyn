---
type: contributing
topic: verification-hazards
audience: [human, agent]
---

# Verification hazards

A co-vet reviewer or test author's checklist for one root failure:
**an observation does not name its own referent.** `rc=0`, `CI: SUCCESS`,
`import reyn` succeeding, a doc's confident prose — each looks like evidence
for a specific claim, but by itself says nothing about *which* claim. Every
hazard below is that root wearing a different substrate, each with a real
2026-07-16/17 instance and a detection technique that actually closed it —
not a theory, a worked example. If a hazard here can't cite a real instance
and a measured detection, it doesn't belong in this doc.

## 1. The five faces of "observation ≠ referent"

| Face | What's missing | Instance |
|---|---|---|
| **Record is a lie** | The claim itself is false | `landlock.py` blamed network denial on "the no-network-fd / proxy gate" — a named mechanism that appears nowhere in the repo but that one comment (#3031). What actually denied `connect()` was a seccomp default-deny, itself skipped under `allow_subprocess=True` (#3030). |
| **Environment can't witness** | A green test never ran the risky path | The Landlock shim called `Ruleset` APIs (`add_path_beneath_rule` etc.) that don't exist in the pinned `landlock==1.0.0.dev5` — every call raised `AttributeError` in production for 41 days, while its own test called the shim's internals directly, bypassing the broken production entry point (#2980). |
| **Claim has no owner** | No one on the claimed subsystem's side checks it | Same `landlock.py` case: a doc/comment in subsystem A asserting subsystem B's behavior, with no owner on B's side to catch it wrong — "plausible and unowned" is why it survived. |
| **Observed-target identity unverified** | Green about the wrong object | Agent worktrees share the main checkout's `.venv` (0 of 136 have their own) — in-process and subprocess-imported `reyn` are two different trees "by construction, not staleness" (#3033). Separately: the same heading anchor resolves to two different slugs on GitHub vs mkdocs — "valid" is renderer-specific (#3039). Separately again: a SHARED venv's editable install can be silently re-pointed to a DIFFERENT worktree by someone else's concurrent `uv pip install -e .` (`VIRTUAL_ENV` resolves to the parent venv from inside a worktree, so the parent's `.pth` re-links to that worktree) — a strip-falsify then measures a tree nobody touched (#3363/#3370, 2026-07-27). Central audit can't catch this: a session can only resolve its OWN `python`'s import, never another session's PATH. The fix is measurement-time self-check, not a periodic audit — `python -c "import reyn; assert reyn.__file__.startswith('$PWD/src')"` before trusting any local strip result, reading the RESOLVED path (`reyn.__file__`), not the DECLARED one (the `.pth` file's contents). CI is structurally exempt (`actions/checkout` + a fresh editable install every run leaves no other tree to point at) — only local strip results are at risk. A second axis of the same identity question, this time TEMPORAL rather than spatial: analyzing a central file (`session.py`) on a shared tree, then applying that analysis later by line number, silently broke when `main` moved underneath the analysis (+107 lines from 3 intervening merges) — the same file at two different times is as much "a different object" as two different worktrees are. Caught only because the applying agent inserted a content-based line-anchor check before applying and got 87/100 mismatches; without that check the file would have been silently corrupted. Recovery reached 381/389 sites, and the 8 unrecoverable ones all fell inside the one region a concurrent merge had actually touched — a mechanistic explanation, not randomness (2026-07-28). Line numbers are not identifiers across a moving `main`; re-anchor by content immediately before applying whenever measurement and use are separated in time. |
| **Run doesn't support the claim** | The execution was real; the CONDITION or the FAILURE wasn't | A flake documented as *load-sensitive* passed 25/25 — run sequentially, one test, on a quiet machine: the condition was avoided, not created, and it read as "fixed" (2026-07-30). Correcting for that by running six copies of the SAME test concurrently produced 17/18 red — all `FileExistsError: agent 'operator' already exists`, six copies fighting over one workspace. That number would have contradicted a peer's bisect, first blamed on an artefact of the harness — but it was a real single-occupancy defect in the test itself (a machine-global base dir it wipes on entry, #3473/#3519): a valid reproduction of a DIFFERENT defect than the one being asked about, not a broken measurement. Only the third attempt — DIFFERENT tests in parallel, the actual CI shape — was evidence for the ORIGINAL question, and it surfaced a third failure face (`No fixture entry for model=…`) distinct from both tracked causes, redirecting a peer who was hunting a symptom that no longer reproduced. Same family, counting rather than conditions: "the residue is 2 cells" counted OCCURRENCES of an SGR escape; one had no following text and SGR runs to end of line, so the number understated it — the count was executed, the unit was never settled (#3504/#3505). A fourth mechanism, same face: green can mean the EXECUTION MODE itself changed the condition. The same race-prone test read 2/30 and 8/30 failed in a fast, in-process regime (short pauses, tight timing window), then 0/20 failed when run the normal way, through `pytest` — the wrapper's own overhead widened the timing window enough to make the race stop landing (#3581). A local `pytest` re-run reading green after a fix therefore proves nothing about the defect; it only shows the race's condition wasn't present in THAT regime, same as the sequential flake above. |

**Apply**: before trusting a green result, name what it actually observed,
not what you're using it to conclude. For a rate or a count, also name the
CONDITION the symptom requires (load, parallelism, a cold cache, a specific
terminal) and whether this run had it — "sequential and quiet" is not "under
load" — and open the failures before they become a number: a red count says
nothing about whether the red is yours. Parallelism must come from DIFFERENT
tests sharing a machine; N copies of one test share its fixtures and fail on
each other. A failure appearing right after a merge is not evidence that
merge caused it until the PARENT commit is run under the same condition — a
"fixing a defect started a failure" story was proposed and shipped here
without that control arm, then falsified by running the parent commits: the
failure predated the merge blamed for it by at least one step (2026-07-31).
Temporal proximity is not causation without a control. When a fix's own
verification runs green in a DIFFERENT execution mode than the one that
found the defect (in-process vs. `pytest`, sequential vs. parallel, local vs.
CI), first identify the failing regime, count the reproduction rate IN that
regime, then recount in that SAME regime after the fix — a green result from
a different regime answers a different question.

## 2. False-capability vs. false-prohibition — the dual, and only one is self-sealing

A false **"X works"** claim dies the first time anyone depends on X — #3037's
hand-rolled fake made a permission gate report CLEAR; production then let an
LLM write `.reyn/config/mcp.yaml` with zero gating, discovered once someone
used the path for real.

A false **"you cannot X"** claim is worse: it forbids the one action that
would falsify it. A RAG skill's `SKILL.md` asserted MCP servers "cannot" be
installed by the LLM — the install tools were registered and reachable the
whole time; nobody had tried, until the owner asked why it was forbidden
(#3036). Prohibitions don't get exercised in normal use, so they don't
self-correct — they need someone to deliberately attempt the forbidden thing.

**Apply**: a "cannot" claim in a doc or design note needs an owner who
actually tried it, not just someone who read the code and inferred it.

## 3. Two zeros: one settles it, one says nothing

Grepping for a **named, specific mechanism** outside the claim that invokes
it: zero hits *is* the answer (a floating, invented reference) — this is how
the "no-network-fd / proxy gate" phrase (§1) was confirmed dead.

Grepping for a **missing field access**: zero hits says nothing, because
`getattr(obj, "field", default)` on a nonexistent field is never written
anywhere to be found — absence isn't a string you can search for. #3037's
invented `permission_resolver` field required an AST diff against
`RouterCallerState.__dataclass_fields__`, not a grep, to surface at all.

A third way zero can lie even when the thing WOULD leave a trace: the search
tool itself silently fails to find it. A hardcoded-color census reported
"only 2 violations," compounding two independent defects: it searched for
the CSS `property: #hex` shape only, never the Python color constants that
were the real majority of the usage; and a follow-up attempt to grep for
those constants directly ALSO returned zero, because `git grep -E` is POSIX
ERE, which does not understand `\s` — so even a known hit
(`_CC_DIM = "#6b7280"`) went unfound. The true count was 8 constants across
66 call sites, one of them (`_CC_DIM`) a live candidate for a real
"gutter text invisible" bug an owner had already reported (#3525, 2026-07-31).
Neither zero was distinguishable from a real zero without recognizing the
query itself was narrow, then broken.

**Apply**: before trusting a "zero hits" result, ask whether the thing you're
checking for would leave a positive trace if present, or only an absence —
only the first kind makes zero a real answer. Then calibrate the instrument
itself: run the same query against one KNOWN hit before trusting a null
result on the rest, and prefer `-P`/PCRE or a portable class like
`[[:space:]]` over ERE metacharacters that vary by grep flavor.

## 4. Census vs. structure — extrapolation dies on use, not on review

A count derived from a partial signal ("N files call `sys.executable`, 7 are
pinned, so 33 must be wrong") is a **census**; it passed three independent
reviews unchallenged because review checks plausibility, which has no
natural zero to hit. The number died only when a migration task forced
someone to actually read what the spawned processes import —
`grep -c "import reyn"` on the real consumer returned **0**, not 33 (#3024).
Spawning a process is not the same claim as that process importing `reyn`;
the census conflated the two.

**Apply**: a count is only as strong as the step that would have to consume
it and find it wrong. If no such step exists yet, the count is a hypothesis,
not a result — say so.

## 5. Gate-ability: closedness of the target, not "structural vs. semantic"

Two structural/AST checks were prototyped as CI gates this session and both
were rejected — not because AST checks are bad, but because of *what* they
targeted:

- A syntactic gate for a sleep-then-assert anti-pattern, run against 19
  flagged sites: **0/12 precision** on inspection — every hit was a
  legitimate settle-window or poll loop (#3034).
- An AST-enum checking for invented dataclass fields, built for one closed
  type (`RouterCallerState`): zero false positives as a one-off. Generalized
  to "any `getattr` on any dataclass," it missed its own motivating bug —
  **13.7% reach** (#3037/#3040).

The dividing line is **whether the check's target type is closed** (a
concrete dataclass with a known field set) or **open** (an unannotated
variable, `Any`, duck-typing). A closed target makes the check structural
and cheap; an open one forces it back to semantic judgment, and precision
collapses the moment it's generalized.

**Apply**: before proposing a structural gate, name the target type and
confirm it's closed. If it isn't, the check is a one-off co-vet spot-check,
not a CI gate — and don't generalize a working one-off without re-measuring.

## 6. The vacuity guard

An enumerated-set test ("for each X, assert Y") passes trivially if the set
of X is empty — the assertion never runs, and the test is silently
worthless. `test_surfaced_gate_claims_match_registered_tool_gates` (#3001)
guards against exactly this with `assert checked > 0, "no surfaced entry
made a gate claim — the regex is probably wrong"` — without it, a silently
broken extraction regex would produce a permanently green, permanently
meaningless test.

**Apply**: any test that iterates a derived set and asserts per-element
needs an explicit non-empty assertion on the set itself.

## 7. Falsifiable-event root: does divergence produce an event?

A faked **callable** bypasses signature-drift detection, but a real call
still raises loudly (`TypeError`) when the contract changes. A faked
**data/state object** has no such backstop: reading an invented field via
`getattr(obj, "field", default)` raises nothing at all — no signature to
drift, no call to fail, just a wrong default forever (#3037; see
[testing.md § Mock vs Fake](testing.md#mock-vs-fake)).

**Apply**: when assessing whether a divergence would be caught, ask whether
it produces a loud event (an exception, a structural diff) or only a silent
default — only the first is actually gated.

## 8. Renderer-specificity: "valid" is surface-specific

The same markdown heading produced two different anchor slugs: GitHub's
renderer converts the space on *each* side of an em-dash to a hyphen
(un-collapsed, so `— ` → `--`), while mkdocs' `toc` extension collapses
consecutive hyphens to one. `mkdocs build --strict` was green — for mkdocs.
GitHub's rendering of the identical file was silently broken (#3039, fixed
by removing the em-dash).

**Apply**: a doc read on more than one surface (GitHub web view, a built
site) needs its interactive elements (anchors, mermaid diagrams) verified
against *each* surface that reads it — a passing build for one says nothing
about the other.

## 9. Input-surface blindness: gate every surface the real mechanism reads

`scripts/check_pr_closing_intent.py` gated the PR **body** against GitHub's
parsed `closingIssuesReferences` — and passed cleanly for PR #3187 (body
correctly said `part of #1909`, `closingIssuesReferences` was empty, checked
and confirmed green before merge). It still auto-closed #1909 on merge: an
intermediate commit's message carried a stray `Closes #1909` left over from
an earlier draft, and GitHub's default squash-merge commit message is the
*concatenation* of all commit messages — a second input surface the gate
never looked at. The green result on the body surface said nothing about
the commit-message surface, because the check's target was narrower than
the real mechanism (GitHub's closing-keyword parser) it was standing in for.

This is the sibling of §8 (renderer-specificity) one layer earlier: §8 is
about a single *artifact* read by two renderers; this is about a single
*mechanism* (GitHub closing an issue on merge) that reads from two
*sources* (PR body, PR commit messages) that a human only edits one of by
habit. A gate that checks the source you *think of* first — the PR body,
because that's where the author writes prose — can be fully green while the
mechanism resolves from a source nobody re-checked.

★ Even the extended (body + commit-message) gate has a residual limit worth
naming explicitly, because the failure mode this section describes is
exactly the one a sloppy reading of the fix would repeat: **this gate
detects a state that CAN still leak a close — it does not, and structurally
cannot, guarantee the close won't happen.** The squash-merge commit message
is only finalized at merge time, and the human merging can hand-edit it —
removing a keyword the gate flagged, or just as easily *adding* one it never
saw. ∴ a green run of this gate means "no leftover closing keyword was
found in the PR-time input surfaces" — it does NOT mean "this PR will not
close the issue." Reading gate-green as close-proof is the exact misreading
that caused the original incident (`closingIssuesReferences` == 0 was read
as "close is prevented," and the issue closed anyway); the fix must not
invite the same misreading one layer up.

**Apply**: before trusting a gate as covering "does X happen", enumerate
every input surface the *real* downstream mechanism actually reads (not
just the one your fix touches) and confirm the gate reads all of them. A
fix that only reruns the check when the touched surface changes (e.g. this
gate previously re-ran only on `edited` — a body-only event) is itself a
symptom: the trigger set silently encodes an assumption about which surface
matters. ★ A pre-merge gate is never itself the observation of the
merge-time outcome — after merging a PR this gate touched, check the
target issue's actual `state` (e.g. `gh issue view <N> --json state`)
rather than treating the gate's earlier green run as the final word; that
issue-state read is the only thing that actually observes what happened at
merge time.

## 10. Born-vacuous: a live mechanism behind a gate that only inspects the terminal state

Distinct from §6 (an *empty set* makes the assertion never run) and §7 (a
*faked collaborator* removes the loud-failure backstop): here the set is
non-empty and the collaborator is real, but the **assertion only inspects
the terminal state, an adjacent path, or mere existence** — so killing the
live mechanism entirely still leaves the gate green. Three independent
instances, all measured the same session (#3288/#3299 streaming + panel
arc):

- **Terminal-state-only assertion** (#3288 ③c). The gate asserted "N deltas
  → exactly one entry whose content is the full text" — but only *after*
  the completion frame arrived. The completion frame alone creates that
  one-entry, full-text result regardless of whether delta-coalescing ever
  ran, so the gate passed with **zero deltas delivered**. Fix: assert a
  **mid-stream cross-section** — before completion arrives, exactly one
  entry exists and its content is *partial*. Zero deltas then produces zero
  entries at that cross-section ⇒ RED, and the arrival witness is
  *subsumed* by the gate itself instead of needing a separate positive
  control.
- **Positive control on a different delivery path** (#3288 ③b). A
  positive-control witness was injected via `_queue.put`, bypassing the
  `push_event` path the production code actually uses — so breaking
  `push_event` left the witness passing and the gate vacuous; it proved
  only that a *neighbouring* path was alive, not the one under test. Fix:
  the positive control must traverse the **same delivery path** as the
  thing being verified. (Worth naming honestly: this wrong prescription
  came from the reviewer in the first pass, and was only caught when the
  coder's deviation from it was surfaced for a ruling instead of being
  silently accepted — a reviewer's gate design is not exempt from this
  hazard.)
- **Existence check instead of the real property** (#3299 P5). A layout
  gate asserted `region.height > 0` and `display` truthiness, and stayed
  green on a build where the intervention panel had visually taken over
  the whole screen — the widgets were not zero-height, they were **pushed
  off-screen** (composer at `y=25` on a 24-row screen; flowview at `y=-8,
  h=1`). An upper-bound-only containment check (`y + h <= screen_height`)
  is equally useless — trivially true for any negative `y`. Fix:
  **bidirectional** containment (`y >= 0` AND `y + h <= screen_height`)
  plus a not-squashed floor on height, parametrized over at least two
  terminal sizes. Related corollary: an `ImportError`-based RED proves a
  reference vanished, not that the value it named ever reached the
  rendered output — existence of a symbol is not existence of the effect.

**Apply**: before shipping any gate, apply it to the known-broken build (the
one the mechanism, if absent, would produce) and confirm it goes RED. A
gate that has never been seen failing on a real defect — only ever seen
green — is unproven, no matter how plausible its assertion reads.

## 11. Strip-anchor must be unique — the measuring instrument, not just the gate, needs falsifying

§10 assumes the gate is sound and only its assertion scope is the problem.
This is a different failure point: the gate IS sound, but the **strip itself
— the instrument used to check it — hit the wrong site**, and reported
vacuity that never existed.

A reviewer stripped `self._running_tools.clear()` and `collapse_all()` to
check whether a gate was load-bearing, got "8 passed," and reported the gate
as vacuous. The coder re-measured and found a mismatch: both anchors were
**non-unique** — `_running_tools.clear()` appears at two call sites (an
orphan-sweep and a switch handler), and so does `collapse_all()` — and
`str.replace(..., 1)` silently patched the FIRST occurrence, which was never
the one under test. Stripping the correct line instead sent both tests RED:
the gate had been sound the whole time (#3310 N2, 2026-07-26).

This is the dangerous direction on purpose: it's a **false positive** against
a healthy gate, not a false negative that lets a broken one through — and
false positives here are harder to catch, because the person told "your gate
is vacuous" usually just rewrites it, rather than pushing back. Three sound
gates were nearly rewritten on the strength of a bad strip.

**Apply**:

- Before stripping, **count the anchor's occurrences**. If `count != 1`,
  target the line number or the enclosing function instead of a bare string
  replace (or widen the anchor until it IS unique).
- `assert s != before` (the string changed) is **not sufficient** — it
  proves *something* mutated, never that the *intended* site did.
- If your strip result disagrees with a peer's, **don't average it — settle
  it**. Strip-falsify is the foundation the rest of the session's RED/GREEN
  reports rest on; an unresolved disagreement here erodes trust in every
  later report.
- If you're on the receiving end of a "your gate is vacuous" finding,
  **re-measure yourself** before rewriting — this incident was only caught
  because the accused party pushed back with a countermeasurement instead of
  complying.

## 12. Coverage has two axes, and enumerating a registry only closes one

A registry-driven coverage gate (mandated by §5's own closed-target
discipline) answers "did I cover everything the registry counts" — nothing
more. A defect can live on an entirely different axis the registry has no
concept of.

`ToolDefinition.render_for_router` returned a SHALLOW copy of a tool's
parameter schema; litellm's provider transforms mutate a handed-out
`tools[]` payload IN PLACE, so one Gemini call permanently corrupted the
canonical schema for every later render, on every provider, for the rest of
the process. The defect itself dates to `edd4c1b3` (2026-05-09, ADR-0026
M1) — roughly two and a half months before discovery on 2026-07-28, not the
16 days a naive reading of the detecting test's own history would suggest
(that test's baseline belonged to an unrelated 2026-07-12 refactor; the
refactor's age is not the defect's age — precisely this doc's own root
hazard, "since when observed" answered in place of "since when true"). The
original coverage gate enumerated THREE seams where a schema leaves its
`ToolDefinition` — live, registry-driven, "a future tool is covered from day
one." **Total on the *tool* axis, partial on the *seam* axis**: a FOURTH
seam, the hot-list direct-alias path, rendered through none of the three and
sat outside a gate that read as exhaustive (#3383/#3385). Enumerating tool
NAMES can never surface this, because the defect lives at the SHAPE of a
projection (`dict(<expr>.parameters)`), not at any name — the fix found the
missing seam only by grepping the codebase for that shape directly.

The same incident sharpens §6 (the vacuity guard) one level further. The
fix's own AST gate — REDs on any `dict(<expr>.parameters)` call outside the
one sanctioned projection helper — is only meaningful while that helper
still does its one job. A **positive companion** assertion (the helper
actually calls `deepcopy`, checked by walking its own AST span) is what
protects the allow-list from going vacuous if the helper is later renamed or
gutted — and this companion is not a formality: it immediately caught the
first draft's allow-list anchor not matching the helper's real call shape,
i.e. it was guarding a chokepoint by a shape the helper didn't use. **An
allow-list gate needs its OWN allow-listed target proven correct by a second,
independent assertion** — "the gate's target exists" is not the same claim
as "the gate's target still does what the gate assumes."

**Apply**: before trusting a registry-enumerated coverage claim, name the
axis the registry actually counts (usually identity/name) and ask whether
the known defect classes live on that axis or a different one (a shape, a
side effect, a shared-mutation seam). If it's a different axis, enumerate
from the SIDE OF THE VALUE (grep the shape the defect takes), not from the
registry. And whenever a gate's allow-list is anchored to a specific
helper/chokepoint, add a companion assertion that the chokepoint still does
its one job — the allow-list's soundness is contingent on that, not
intrinsic to it.

## 13. A gate can prove the property that's easy to state, and never touch the property that matters

Three same-day instances (2026-07-26/28) share one shape: the property a
test asserts is the one that was **convenient to write**, and it is a
strictly NARROWER claim than the property the docstring or the design
actually promises.

- **A reserved-key gate** declared two properties — "no live binding
  collides with a reserved key" AND "a reserved key cannot be silently
  claimed by a new binding" — but the code only asserted the first. The
  reserved-key table was folded into the same set as live bindings and only
  membership-tested against two specific keys; the table itself was never
  the SUBJECT of an assertion, only an ingredient used to fatten another
  set. The tell: **a named set that appears only on the input side of
  another set's construction, never asserted about directly, is a property
  that looks wired but isn't** (#3363, the sent-queue `RESERVED_KEYS` case).
- **A two-declaration list-match** (A2A's and MCP's progress fan-outs both
  declare the identical three-kind `TRACKED_EVENTS`) was the easy property,
  and it WAS gated: `test_a2a_progress_bridge_tracks_three_lifecycle_events`
  asserted the two literals matched each other and pinned their exact
  contents. "Every declared kind actually has a live emitter" was the
  property that mattered, and nobody asserted it: two of the three kinds had
  zero producers in `src/` for an unknown period, silently degrading two
  live network protocols' progress streams to LLM-call-only (#3357). Worse,
  the literal-pin test actively **resisted** the correct fix (removing the
  dead entries) rather than merely failing to catch the defect — the fix
  had to touch a test that read as unrelated, easy-to-miss friction. #3389
  replaced the pin with a liveness gate instead
  (`test_every_forwarded_kind_has_a_live_emit_call_site`: every declared
  kind must have a real `emit("<kind>", …)` call site in the source) — and
  this doc's own draft aged out in the meantime: the paragraph above was
  accurate when written and became false by the time it landed, the exact
  failure this doc is about. Read it as history, not current state.
- **§12's four-seam behavioral coverage** is itself an instance of this
  shape one level up: "the seams that exist all route through the
  projection helper" is provable and was proven; "no NEW seam can ever
  route around it" is a different, harder claim that needed a structurally
  different assertion (the AST gate), not a bigger version of the same one.

**Apply**: when writing or reviewing a gate, write out the FULL property the
surrounding prose/docstring promises, then check whether the assertion
covers all of it or only the half that was easiest to construct. A specific
tell: if a named table/set/list is read only to feed another assertion —
never itself the direct subject of one — it looks wired but is unverified.
And before a gate is allowed to pin a literal collection's exact contents,
confirm that pinning it won't fight the next correct change to that
collection.

## See also

- [Testing policy](testing.md) — Tier model, Mock vs Fake, decision flow.
- [CLAUDE.md](../../../../CLAUDE.md) — the doc-sync hard rule (a doc
  describing a mechanism goes stale the moment the mechanism changes) is the
  same family: a claim whose referent moved out from under it.
