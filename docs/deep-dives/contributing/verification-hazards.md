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
| **Environment can't witness** | A green test never ran the risky path | The Landlock shim called `Ruleset` APIs (`add_path_beneath_rule` etc.) that don't exist in the pinned `landlock==1.0.0.dev5` — every call raised `AttributeError` in production for 41 days, while its own test called the shim's internals directly, bypassing the broken production entry point (#2980). A repro can fail the SAME way a test can: investigating whether a secret leaks through `Session._run_router_loop`'s TUI/outbox path (#3830), a reproduction was built that raised the exception WITHOUT going through `scrub_exception_in_place` — the then-production path (removed by #4353; the leak surface itself was also closed by #4348) — and reported the fixed code as still leaking. Self-corrected once rebuilt to raise through the real call path, which produced the REDACTED text the first repro never could have shown either way (#4343). The repro environment, not a test, was the thing that couldn't witness the real path this time. |
| **Claim has no owner** | No one on the claimed subsystem's side checks it | Same `landlock.py` case: a doc/comment in subsystem A asserting subsystem B's behavior, with no owner on B's side to catch it wrong — "plausible and unowned" is why it survived. CLAUDE.md asserted the sandbox 3-tuple axis contract (deny/exception-boundary/workload) was "CI-conformance-only" — a claim about subsystem B (CI) written in subsystem A (the hard-rules doc) — with no one on B's side ever checking it was true: `test_sandbox_axis_contract_2983.py`'s network-axis arms skipped in every pytest job (no `sandbox-linux` extra) and were never collected by the one job that DID have Landlock available, so the "CI-conformance" layer ran on zero jobs, not the CI-conformance-only-but-still-real coverage the sentence implied. Distinct from the "environment can't witness" row above: the deny-gate job's environment COULD witness the risky path fine — once the file was added to its `pytest` invocation it ran and passed (34/34) — the gap was never a capability gap, only that nothing on the CI side had ever checked the claim against what actually ran (#4333, found via #4331's skip census cross-referenced against which job collects which file). |
| **Observed-target identity unverified** | Green about the wrong object | Agent worktrees share the main checkout's `.venv` (0 of 136 have their own) — in-process and subprocess-imported `reyn` are two different trees "by construction, not staleness" (#3033). Separately: the same heading anchor resolves to two different slugs on GitHub vs mkdocs — "valid" is renderer-specific (#3039). Separately again: a SHARED venv's editable install can be silently re-pointed to a DIFFERENT worktree by someone else's concurrent `uv pip install -e .` (`VIRTUAL_ENV` resolves to the parent venv from inside a worktree, so the parent's `.pth` re-links to that worktree) — a strip-falsify then measures a tree nobody touched (#3363/#3370, 2026-07-27). Central audit can't catch this: a session can only resolve its OWN `python`'s import, never another session's PATH. The fix is measurement-time self-check, not a periodic audit — `python -c "import reyn; assert reyn.__file__.startswith('$PWD/src')"` before trusting any local strip result, reading the RESOLVED path (`reyn.__file__`), not the DECLARED one (the `.pth` file's contents). CI is structurally exempt (`actions/checkout` + a fresh editable install every run leaves no other tree to point at) — only local strip results are at risk. A second axis of the same identity question, this time TEMPORAL rather than spatial: analyzing a central file (`session.py`) on a shared tree, then applying that analysis later by line number, silently broke when `main` moved underneath the analysis (+107 lines from 3 intervening merges) — the same file at two different times is as much "a different object" as two different worktrees are. Caught only because the applying agent inserted a content-based line-anchor check before applying and got 87/100 mismatches; without that check the file would have been silently corrupted. Recovery reached 381/389 sites, and the 8 unrecoverable ones all fell inside the one region a concurrent merge had actually touched — a mechanistic explanation, not randomness (2026-07-28). Line numbers are not identifiers across a moving `main`; re-anchor by content immediately before applying whenever measurement and use are separated in time. A third axis, this time about the isolation boundary itself rather than what's inside it: a session's dedicated worktree can vanish MID-SESSION (the cause was never established — only that a registered tree later no longer existed), and because a tool-calling agent's shell `cwd` resets between calls, every subsequent command silently fell back to the shared checkout instead of erroring — a commit believed isolated was made on the shared tree (#3581). "Worktree-isolated" is a fact true at session START, not an invariant that holds for its duration. The only way to catch this is a self-check at the moment of measuring, committing, or pushing — `pwd`, `git worktree list`, and venv/`reyn.__file__` identity — not trusting that isolation, once true, stays true. |
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

The other side of the same coin: **a nonzero hit is not itself an answer
either** — it's a classification that isn't decided until the hit is opened.
Auditing whether two issues were already landed, one search returned zero
hits and correctly meant "not started" (#4285) — but the SAME method applied
to a second issue returned one hit and was classified, from the search
result alone, as "just a mention in context," without opening the PR
(#4287). The PR had actually landed dead-code removal, a test retarget, and
a falsify record — all of it, complete, sitting behind that one hit. Running
the check was not the safety measure; reading the hit as evidence was. Zero
and nonzero are not symmetric: **zero can settle a question (once you've
confirmed it's the searchable kind, above); a nonzero count never settles
anything by itself — it only earns the right to open the result and read it.**

**Apply**: before trusting a "zero hits" result, ask whether the thing you're
checking for would leave a positive trace if present, or only an absence —
only the first kind makes zero a real answer. Then calibrate the instrument
itself: run the same query against one KNOWN hit before trusting a null
result on the rest, and prefer `-P`/PCRE or a portable class like
`[[:space:]]` over ERE metacharacters that vary by grep flavor. And when the
count comes back nonzero, don't stop at the count — open every hit and read
it before classifying it as "just a mention"; the classification isn't
decided by the search, only by what's actually in the hit.

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

**Vacuity doesn't require an empty set — a RANDOM one can be just as silent.**
A retry test's own docstring claimed "a failed build's pool retries with a
different effect" and drew its 2-item pool via `random.sample` — an exact
permutation of a 2-item list, so "the failing effect lands first" (the
precondition the claim needed to be exercised) held only HALF the time.
The other half, the real effect sampled first, retry-after-failure never
ran, and the test passed anyway — "some effect eventually varies" is true
either way, so the assertion couldn't tell the two runs apart. Compounding
it: the file carried `@requires_tte`, so CI (no `effects` extra) skipped
the whole file regardless — a coin-flip-vacuous test that was ALSO never
run. The fix monkeypatched `random.sample` to preserve list order (the
failing effect deterministically first, every run) and replaced the real
TTE-effect pool member with a same-contract test double, which incidentally
also removed the `@requires_tte` skip (#4291/#4330).

**Apply**: any test that iterates a derived set and asserts per-element
needs an explicit non-empty assertion on the set itself. When a test's own
precondition is drawn by randomness (a `random.sample`/`random.choice` over
a small pool), ask what fraction of runs actually exercise the claimed
scenario — a "sometimes runs the interesting case" test is vacuous on every
OTHER run, silently, the same as an empty set is. Pin the ordering
(monkeypatch the random draw) rather than accepting a probabilistic
precondition, and check the file isn't ALSO structurally unreachable in CI
(an optional-extra skip marker) — a fixed vacuity inside a skipped file is
still a vacuous test, just one layer further from being seen.

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

This trap doesn't need a gate to recur — the same asymmetry lives in a
conclusion, not just in code: investigating whether a tool was reachable
under a config mode, a reviewer enumerated ONE seam that strips entries
(`ExposureDeviation.excluded_names`, six module-level construction sites,
the tool in none of them) and concluded the tool was reachable — a
conclusion about the *seam axis* drawn from counting one seam. A second,
architecturally separate strip seam existed one layer away
(`_WRAPPER_SUPERSEDED_BASE_TOOLS` in `router_tools.py`, #3429) and DID
name the tool. The retraction's own words: "`excluded_names` が唯一の
seam」と書いたのは私の誤りです。registry を1本列挙して、seamの軸について
結論しました" (#3896) — this doc already named the exact shape, and having
read it did not stop the same session from doing it anyway, because
nothing in the moment of writing "I enumerated the seam" asked whether
"the seam" meant *a* seam or *every* seam.

🔴 **Active trigger**: before writing "I counted/enumerated all the X," stop
and say out loud which axis X names — is X *the* seam/registry/mechanism,
or *a* seam/registry/mechanism among others that might exist? A silent
axis (the one you didn't have to name because you only found one instance
of it) is the one this section is about.

⚠️ **This trigger's own force is grammatical, and grammar is language-
specific.** "*The* seam" vs. "*a* seam" forces the axis question into the
open because English requires an article either way — there is no
article-free way to write the sentence. A language without articles (仮に
日本語で書けば「seam を列挙した」) can state the same finding with the axis
question left completely unmarked; nothing in the sentence's grammar forces
it to surface, so the trigger has nothing to catch. #3896's own finding was
written in Japanese — the language of the person this trigger would most
need to reach the moment it existed. Writing in a language without
articles: say explicitly whether it is 「唯一の X」("the only X — checked
that no other exists") or 「X の一つ」("one of possibly several X — did not
check for others") — the distinction the article carries for free in
English has to be spelled out by hand where grammar won't carry it.

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
- **A migration-safety gate's founding axiom** (2026-08-09/10,
  `check_migration_diff_shape.py`, #3995→#4002→#4009): "byte-identical
  `R100` rename ⇒ safe" is a provable, easy-to-state property, and CI
  enforced it correctly. It is a narrower claim than "this file's behavior
  is unchanged by the move" — false in principle for any
  `Path(__file__)`-rooted expression, whose meaning depends on the file's
  OWN location. The design went through two retracted attempts to widen the
  SAME predicate before landing on the right shape: first, "flag by
  hop-count" (retracted — `Path(__file__).parent / "_support"` is one hop
  but still breaks on a move, because `_support` doesn't travel with the
  file); second, "classify by whether the target travels with the file"
  (retracted — that's a property of the MOVE, not the source text,
  undecidable statically). What shipped instead is **two separate,
  narrower mechanisms**, not one bigger predicate: a move-time check that
  re-resolves every `__file__` expression against the file's REAL new
  location (no inference needed, the move already happened), and a
  static add-time check restricted to a filesystem-derived structural
  proxy (does the expression reach `tests/` or one of its current direct
  children) — deliberately not attempting the undecidable general claim.
  Both new gates then found 30 real, pre-existing instances of the defect
  class on their first whole-tree run, confirming "declared byte-identical
  ⇒ safe" had been true only for the specific property it checked, never
  the broader one readers assumed from its name.
  **Update (2026-08-10, #4069→#4073)**: the axiom itself did not loosen —
  a reference that CANNOT be written correctly before its own move lands
  (an `import` of the moved module's dotted path, a `path.is_file()`
  registry string) has no other legal order to land in, so permitting it
  is not a bigger version of "byte-identical ⇒ safe", it is a mechanical
  identification of the subset that was never byte-identical-capable in
  the first place. The check builds the PR's own rename mapping from its
  `R100` lines and verifies every changed line, hunk-scoped, resolves
  under that mapping — no external list, no human judgment call. This is
  the opposite direction from #4064's rejected proposal (widen rule ① to
  "any coherent subject", a human judgment call): #4073 substitutes a
  mechanical check for what could previously only merge as a declared,
  unverifiable "a human read every line" promise.
  **Retired (#4492)**: the gate itself — `.github/workflows/migration-diff-shape-gate.yml`
  and `scripts/check_migration_diff_shape.py` — was removed once #3879
  Stage 1 completed (its own `removed_by` condition). This bullet stays as
  history of the design lesson; the script it names no longer exists on
  disk. Same instinct this doc's own hazards are about — a past-tense
  account is not automatically false, but a reader following the name
  needs the one line saying where it went.

**Apply**: when writing or reviewing a gate, write out the FULL property the
surrounding prose/docstring promises, then check whether the assertion
covers all of it or only the half that was easiest to construct. A specific
tell: if a named table/set/list is read only to feed another assertion —
never itself the direct subject of one — it looks wired but is unverified.
And before a gate is allowed to pin a literal collection's exact contents,
confirm that pinning it won't fight the next correct change to that
collection.

## 14. A declaration of completion is not a witness of completion

A lead reported an arc's items "①②④⑤ complete" after merging a PR. Item ①
explicitly included a rewrite of the LLM-facing tool-description prose for a
retired mechanism. A later PR's author, editing a nearby paragraph, checked
`gh pr view <N> --json files` for the merged PR before citing it as
precedent — and that file was not in its file set. The retired mechanism's
sales pitch (`"returns a structured preview... and a path_ref to the full
body saved under .reyn/tool-results/"`) had shipped to the model, unchanged,
for the entire duration between the two PRs: "①complete" was true only for
the item's *code* half, not its doc half, and the declaration didn't
distinguish the two.

This is the completion-declaration form of the same root as §1's "Observed-
target identity unverified" — not "which object did I measure," but "which
object did the PERSON REPORTING COMPLETION measure before declaring it."
Reporting "arc done" is itself a claim, and the claim's own referent (the
actual file set of the PR being cited) was never checked before it propagated
into a second person's mental model of what was already fixed.

A sibling instance, same day: an owner asked why four constructor params
weren't grouped together. A lead re-derived the grouping criterion from the
code, doubted it under owner questioning, and re-measured to back up the
doubt — a full four-step chain, all of it already sitting in PR #3515,
merged two days earlier, including the exact rejected alternative and the
measured reason it was rejected (a looser criterion looked cleaner but
silently mis-grouped an unrelated param). The habit of grepping for an existing
mechanism before writing a new one had been applied to CODE but not to the
DECISION RECORD — the PR body, issue comments, and design firms that already
settled the question. `gh pr view <N> --json body` would have closed it in
one command.

**Apply**: before citing a prior PR as having landed item X, run
`gh pr view <N> --json files` and confirm X's own named file is actually in
that set — a declaration of completion is not a witness of completion, and
checking costs one command. Before re-deriving *why* a design is the way it
is — especially when a criterion starts to look wrong under questioning —
search the decision record (`gh pr list --search "#<issue> in:body"`,
`gh pr view <N> --json body`, `gh issue view <N> --comments`) for the PR or
issue that already settled it, including any rejected alternative and the
reason it was rejected; re-deriving from code alone re-litigates a decision
someone already made and recorded.

## 15. A declared field, a complete implementation, and a passing test all look alive — regardless of whether anything calls them

`MemoryService.remember` / `.forget` / `.read_body` were fully implemented,
docstringed, and one of them had a Tier-2 test that explicitly asserted it
was exercised by "a live consumer." Their actual production caller count was
**zero** — three near-duplicate router-loop privates did the real work,
reimplementing the same rules (a threat-scan reject, frontmatter
construction, index ingest) independently. `RouterCallerState.memory_service`
existed as a declared field for the same reason: wired by nobody, because the
wiring had gone to three separate `*_fn` callables instead (#3608).

Worse inside the same defect: a security check with a HAND-MIRRORED test
double has a test path that never touches the real thing. Two call sites
copied `scan_for_block`'s scan-then-emit sequence by hand into fake host
objects (`_BlockHost`) rather than driving the real method — so the test
suite could go green forever even if the real `scan_for_block` diverged from
its copies, because nothing ever asserted the copies matched.

A field, an implementation, and a test all being present is necessary for
"this is live" but nowhere near sufficient — none of the three states whether
anything on the production path actually calls it. This is a different axis
from §1 (which object a measurement touches) and §14 (whether a completion
claim's own referent was checked): here the artifact itself is completely
real and correctly built, and the gap is that its EXISTENCE was mistaken for
its USE.

**Apply**: when a mechanism looks fully built — field declared, method
implemented with a docstring, test passing — count its production callers
before trusting that it does anything. Existence of the field, the
implementation, or the test does not count toward that number. Also check
whether a test that exercises "the same behavior" does so through the REAL
call path or a hand-mirrored copy of it — a copy that never diverges from
the real thing under test is not proof they stayed in sync, only that no one
has yet made them diverge.

## 16. An equality assertion is blind to both sides being wrong the same way

A PR unifying two code paths ("door A" and "door B" build the same object)
naturally reaches for an equivalence test: build via both doors, assert the
results match. That assertion is structurally unable to catch the case where
BOTH doors are wrong IDENTICALLY. A strip that regressed one door's
`agent_id` back to `None` left the equality arm GREEN — both doors produced
`None`, so they still "agreed" — while a separate identity-specific
assertion (`test_the_agent_identity_reaches_the_registry_dispatch_door`)
went RED, because it asserted the actual VALUE reaching a consumer, not
whether the two doors matched each other (#3610).

Two arms are required for this class of unification test, not one: an
equality arm (do the two paths still produce the same thing, guarded
against vacuity — a real change must show through the fingerprint) AND at
least one VALUE arm that asserts a specific field reaches a specific
consumer with a specific expected value, independent of the other door
entirely. The equality arm alone cannot distinguish "both doors correct" from
"both doors wrong in the same way" — a value arm is the only assertion form
that can.

**Apply**: when testing "these two paths build equivalent output," don't
stop at the equality assertion. Add at least one assertion that names an
actual expected value for a load-bearing field, reached through only ONE of
the paths, independent of whatever the other path currently produces.

## 17. Scope, not just pattern — a grep's directory/file-set decides the census as much as its regex does

Distinguish this from §3/§4 (a pattern too narrow in TOKEN SPELLING — a CSS
`#hex` shape missing a Python `_CC_DIM = "#6b7280"` constant, or POSIX ERE not
understanding `\s`): here the regex can be exactly right and the census still
undercounts, because the search never looked in the file, or the package,
the defect actually lives in. The searcher reports what matched honestly;
the files never searched produce no signal at all, so a partial SCOPE reads
exactly like a total one — to the searcher and to the reader, alike.

Two same-day instances (2026-08-02):

- A filed defect asserted "no processing restores the cursor position after
  paint (grep 0 hits)." The restore call existed the whole time, in
  `app.py`: `Control.move_to(*cursor_position)` at the end of every frame.
  The fix PR's own retrospective names the actual miss: "`_compositor.py`
  だけを見たための誤りで、復帰は `app.py` 側にありました" ("the mistake was
  from looking at only `_compositor.py`; the restore lives on the `app.py`
  side"). The filed issue's root cause ("doesn't restore") was wrong; the
  real defect was "restores to a STALE value" — a different fix entirely
  (#3621 → #3622).
- A `$text-muted` census ran a grep scoped to reyn's own chat interface
  directory, found 6 sites, and reported that as the affected set. The
  coder's own correction: "私は `grep '\$text-muted'
  src/reyn/interfaces/inline/textual_chat/*.py` で列挙しました。∴ 答えられ
  るのは『reyn が `\$text-muted` と書いた場所』だけです" — the grep's scope
  was reyn's own source tree, and the same collapsed-to-`$text`-value defect
  class also lives in Textual's own `DEFAULT_CSS` (`_text_area.py`,
  `_input.py`, `_option_list.py`) — a directory the census never entered
  because it isn't reyn's code, and one of those sites was already the
  confirmed cause of a distinct, previously-fixed bug in the same class
  (#3523).

### The detection technique

Neither closure came from a better regex. Instance 1 was caught by
searching OUTSIDE the file set the original diagnosis had named. Instance 2
was caught by asking "does this defect class exist somewhere I didn't
look, for a reason unrelated to how it's spelled" — the dependency's own
source tree, not `reyn`'s — rather than widening the pattern.

**Apply**: state scope alongside the count — "N, over `<files/dirs
searched>`" — an unscoped N reads as total. Before trusting a 0 or a small
N, name every location the defect COULD live (adjacent files in the same
subsystem, a dependency's own source, a sibling directory) and confirm each
was actually searched, not just the one that felt likely. Concretely for a
consumer sweep of `src/` construction sites: the population is `src/` /
`tests/` / `scripts/`, not just the first two — `scripts/` runs CI-only
probes that import `src/` without being called either "implementation" or
"test," which is exactly why a sweep worded as "across src and tests" keeps
skipping it (three independent same-night sessions, 2026-08-09, each
scoped a sweep to src+tests, and each hit the same `scripts/` file in CI —
the miss was never a bad regex, only ever the file-set).

## 18. A claim's subject can fail to hold up three different ways — existence, identity, and effect need different detection

The following shape recurred across several independent sessions and layers
on 2026-08-06. The exact count that surfaced it isn't the point — how many
turned up depends entirely on who was looking and at what, which is itself
§17's hazard turned on this section: **describe the recurring shape, not a
census of it.**

A first framing tried to unify all of it as "the check verified the CONTENT
of a claim without verifying the claim's own PREMISE exists" — close, but
too coarse to prescribe anything: it collapses onto three distinct failure
sites, each needing a different detection technique, and folding them
together makes the prescription disappear along with the distinction.

| Shape | What's actually wrong | Detection technique |
|---|---|---|
| **A. Absence** | The named thing — a caller, a method, an attribute, a keyword — does not exist at all | Symbol resolution (AST, a type checker). Grep cannot do this: it reports what matched, and a thing that was never written produces no non-match to report either — silence looks identical whether the thing is absent or merely unsearched (§17) |
| **B. Misidentification** | Something real was measured, but it isn't the thing the claim is about | Prove the identity of what's being measured — version, tree, file kind — *before* trusting what it reports, not after a result looks wrong |
| **C. Inert** | The right, real object was found, but it has no observable effect | Strip it and watch for a change. A generic gate for "this declaration does nothing" can't exist (the property is semantic, not structural — see the closing note below); a gate for one *specific* inert-declaration shape can |

### A. Absence

- `ActivityRow.tick()` was defined but had zero callers anywhere in `src` or
  `tests` — the elapsed clock only advanced as a side effect of
  `specialise()` on each streamed delta, so it froze during tool execution
  and after the stream ended (#3713, caught in review by `git grep -n
  'tick(' -- src tests` turning up only the definition).
- A `compact_caps` method call landed in `app.py` while the method itself
  was never added — the author's own account: "I anchored the edit on a
  name that exists only on another branch, so the call sites landed and the
  method did not. `ruff` passes on a call to a method that does not exist;
  only running it showed the `AttributeError`" (#3724).
- `fv.cursor` was read as an existence check ("does the cursor API work") —
  it returned `None`, misread as "the feature doesn't work," when the real
  fact was simpler: 0.12.0 renamed the whole cursor model, and `.cursor`
  was never an attribute on this version at all (`action_highlight_*` is
  the real surface) — reading a nonexistent attribute returns `None` the
  same way a real-but-empty one would, so the absence was invisible at the
  read site (#3692, still open pending a real keypress measurement).
- A PR's body was missing the `Closes #3716` keyword it needed —
  `closingIssuesReferences` came back empty. The reviewer's own account of
  the miss: "I checked whether the keyword was *valid*, not whether it was
  *present*" (#3722/#3716) — a check aimed at the wrong absence.

Two of these (`compact_caps`, `fv.cursor`) are exactly what a type checker
exists to catch — a call/read against a symbol the checker can prove
doesn't exist on the target type. `pyproject.toml` had `[tool.mypy]`
configured since before any of this, with no CI job ever running it — a
declared check with no execution is the same shape as everything else in
this doc, one layer up, in the CI configuration itself (#3726). Landing it
hit a real first obstacle: mypy aborted on `config/root.py` with `Invalid
syntax`, on a file that parses fine under `ast.parse` — not a real syntax
error, an unidentified mypy-specific one, left named rather than guessed at
(#3728 unblocked it). The resulting ratchet (`scripts/mypy_ratchet.py`,
`(file, error-code)` pairs against a baseline, new pairs fail CI) writes
its stop condition into the FAILING MESSAGE ITSELF, not only its docstring
— the place a reader actually receives it inside a red report, per
architect's co-vet correction that this is the sharper form:

> "\[syntax\] above is not "one more red": a fatal parse error stops mypy's
> ENTIRE run, so no other file was actually checked this time — every OTHER
> pair this run's output doesn't mention is UNMEASURED, not confirmed
> clean. Fix the \[syntax\] finding first; nothing else this run says can
> be trusted until it's gone."

`--write-baseline` also refuses outright when a `[syntax]` pair is present,
rather than silently baking a permanently-truncated run in as "clean"
forever (verified live: injecting a `# type:`-prefixed prose line and
running the ratchet FAILED with exactly this message, and a baselined
`[syntax]` pair still fails on every subsequent run rather than reading as
known debt).

The proposition that quote states — **a run that did not run is not clean**
— has a strictly larger instance the same script did not cover for another
two years: mypy not installed at all (#4576). `python -m mypy` then writes
"No module named mypy" to stderr and exits 1; `run_mypy` deliberately does
not raise on a non-zero exit (mypy's own error exit is the common case), no
`[code]` lines parse out, and zero measured pairs minus the baseline is zero
new pairs. The script printed `mypy ratchet OK: 0 findings, all baselined
(215 declared)` and exited 0 — the `215 declared` supplied by a baseline load
that HAD succeeded, so nothing in the line looked degraded. It hid a real
`[call-arg]` through a complete local pre-PR check (#4575), and was found
only because that PR's CI disagreed with its author's green.

Two things are worth taking from it beyond the fix. First, the author had
already reasoned about truncated runs — the `[syntax]` guard above is that
reasoning — so this was not an unconsidered case but an unsearched *shape*:
"ran and stopped early" was looked for, "never ran" was not. Second, the
general proposition was written down, here, in this document, and neither
the PR author nor its co-vet reviewer reached it. A hazard catalogue is only
consulted at the moment someone suspects a hazard; it does not fire on its
own. The guard added instead is structural — `importlib.util.find_spec`
before the run, so the question asked is "is the tool present", not "does
the output look like a real run", which would have put the discriminator on
the side being classified (mypy's own summary wording, which mypy may
change at will). The exit code cannot serve either: a missing module and a
normal findings-reported run both exit 1 (measured).

The `tick()`/`compact_caps`/`fv.cursor`/`Closes` shape itself — "prove the
named symbol resolves" as a class, beyond what mypy already covers for
method/attribute calls — is **not yet machinized**, not because it can't
be: a caller-count-zero check is exactly what an AST closure test already
does for a different defect (#3714, enumerating every `Path(".reyn")`-style
site against an explicit allowlist so a new stray site or a reverted fix
both fail loud) — the same technique, unbuilt for this particular symbol
class. That same closure test's own blind spot is instructive: it verifies
literal syntactic patterns, so a call that reaches the identical unresolved
premise through a DIFFERENT shape — `list_entries()`/`find_one()` called
with no arguments at all, defaulting internally to the same cwd-relative
path the closure test's patterns were built to catch — passed through
unseen, because it never wrote the literal expression being scanned for
(#3721). Closure-by-AST closes the pattern it enumerates, not the defect
class the pattern was standing in for. The `Closes #N` keyword case already
HAS a gate (`scripts/check_pr_closing_intent.py`, comparing a PR body's
declared intent against GitHub's own `closingIssuesReferences` parse) — its
absence here was a human declining to run/trust the existing gate, not a
missing one.

### B. Misidentification

- A completeness-claiming gate script did `"@quiet@" in path.read_text(...)`
  — a raw substring match over the whole file — and false-fired on its own
  fix's explanatory code comment (`# color: @quiet@, and measured...`),
  counting prose as a live declaration. The file's own `_colour_values()`
  helper already skipped comment/docstring lines; the new gate bypassed
  that guard by reading raw text instead of going through it (#3718). The
  same day, a different PR chose AST over grep for an analogous reason,
  explicitly to avoid "false-triggering on comment/docstring mentions"
  (#3714) — both the trap and its avoidance landed the same day, in the
  same repo.
- Four independent sessions measured against a **mis-pinned**
  `textual-flowview`: two had it frozen at an old version (0.8.0/0.9.0
  against a 0.12.0 pin), and a fourth had the right *version* but was
  reading a local working copy — invisible to a version-only check, since
  the version string can agree while the actual code doesn't (#3723).
  Concrete cost: one session nearly reported a real regression as
  "pre-existing on `main`"; another (this session) wrote an incorrect
  "existing defect on main" characterization into a merged PR's own test
  plan, later corrected. The fix (`scripts/verify_env_identity.py`, #3723
  → #3725) reads `importlib.metadata`'s `direct_url.json` and the package's
  actual import origin — never imports the package to ask it about itself
  — and derives the expected commit from `pyproject.toml`'s own pin string
  rather than hardcoding it (a hardcoded expectation would drift the next
  time the pin moves, reproducing this exact hazard one layer up). A
  session-scoped autouse fixture aborts the *whole run* via `pytest.exit()`
  on mismatch, not one failing test, so a stale pin can't be a single red
  a busy session scrolls past. `local-copy` (version matches, provenance
  doesn't) is deliberately NOT a silent pass: it requires a non-empty
  `REYN_FLOWVIEW_LOCAL_COPY="<reason>"` to downgrade from abort to a
  `warnings.warn()` that lands in pytest's own summary next to the run it
  qualifies — an unconditional abort here was tried first and rejected,
  because "not itself forbidden" needs a way to say so, not just a bigger
  hammer.
- The COMMAND used to inspect a file can decide, by itself, whether the
  version actually read gets recorded anywhere. `git show origin/main:
  <path>` names the ref in both the command and (implicitly) the output —
  self-documenting. `python3 <path>` run directly names no ref at all: it
  silently reads the WORKING TREE, and "I ran it and confirmed" carries no
  record of which version that was (#4215). Same root as the mis-pinned-
  flowview bullet above — a result whose provenance was never captured —
  but the fix here is choosing the inspecting command itself, not adding a
  separate identity-check step: prefer a command whose own invocation
  states the ref over one that defaults to an ambient, unstated state.

**This is where B combines with §16**, not a coincidence: the sessions that
trusted a stale environment did so *because* their result matched `main`'s
— an equality read as confirmation, when both sides were victims of the
identical stale pin. §16's missing premise is *independence* (are the two
things being compared able to fail differently); B's missing premise is
*identity* (is the thing being measured the thing the claim is about) — but
a stale-pin B instance and a matches-main §16 read are the same event
looked at from two premises, and either alone explains why nobody caught it
sooner.

Misidentification isn't confined to code. The same day, one reviewing
session logged eleven separate instrument misses of the same shape, several
during this section's own preparation — each returned `0`, and in every
case the `0` meant "didn't look," not "isn't there": a `from ... import`
binding that a `monkeypatch` couldn't reach because the patch target and
the imported name were never the same object; `path.open()` calls that
don't route through a `builtins.open` patch, because the method resolves on
the `Path` type, not the builtin; a character class silently dropping `_`
from what it was meant to match; a function-name filter that excluded any
call site using the keyword form (`inbox_kind=...`) because the filter only
matched positional call shapes. None of these are exotic — each is a
plausible-looking probe that quietly measures less than it claims to, and
the `0` it returns is indistinguishable, at the call site, from a genuine
absence.

### C. Inert

`ActivityRow` declared `color: @quiet@` — a real, correctly-typed CSS
declaration on the right widget. Measured under the `ansi-dark` theme, it
resolved to the exact same `Color` value as ordinary body text: the
declaration existed, named the right object, and did nothing (#3718 — the
same #3523/#3686 lineage already covered in §9's neighbourhood of this
doc). The fix removed the declaration outright rather than swapping to a
different token, because the row exists specifically to stay visible while
a turn is live — "recede less" was the wrong instinct for this widget, not
just the wrong shade.

**This one is architecturally different from A and B, and the section
would be dishonest to present it as "just not machinized yet."** A general
gate for "this style declaration has no effect" can't exist as one check:
whether a declaration is inert depends on the full cascade, the active
theme, and which OTHER declaration is already producing the visible effect
— a semantic question, not a structural one, and a semantic class doesn't
reduce to a low-false-positive gate — a review co-vet's own recurring
ruling on this exact point. What #3370 built and this domain reuses is
narrower and does work: a foreground/background
contrast-floor check evaluated on the RESOLVED value, after theme
resolution, not on the source token — a real, computable comparison for
"can a human read this," which is a strictly smaller question than "does
this declaration do anything at all."

**Apply**: before trusting a claim, ask which of the three premises it
depends on is actually established — that the named thing EXISTS (resolve
the symbol, don't grep for it), that what was MEASURED is the thing the
claim is about (prove identity — version, tree, kind — before trusting the
result), or that a real, correctly-identified thing has an observable
EFFECT (strip it and watch for a change, in the narrowest domain where that
comparison is computable — not as a general claim). A `0` or a clean run
answers only the one you actually checked; treat an unestablished premise
as unmeasured, not as confirmed absent.

## 19. Documenting an in-flight mechanism as absent invites a same-night rewrite

2026-08-09: a single false claim (`--grant-file-write` "is bounded by the
sandbox write-paths") recurred across four faces, discovered one at a time
rather than enumerated up front — each fix believed it was the last:

```
① src comment (#3916)         "sandbox bounds it" → "no scope of its own"
② docs/reference/cli/* (#3938) same claim, same fix
③ argparse --help text (#3943) same claim, same fix — found only while
                                re-grepping for ① and ②, not anticipated
🔴 ④ #3925/#3942 landed        the FIX ITSELF ("no scope … doesn't exist
                                yet") went stale within the hour — ①②③ all
                                needed a second edit, to "scoped to the
                                zone root"
```

Each individual fix was correct *and verified* at the time it was written —
this is not §1's "declaration ≠ reality" (nothing was declared falsely) and
not §17's scope hazard (every face that was searched for was found). The
failure is temporal: **"doesn't exist yet" is a claim about the state of an
in-flight mechanism, and an in-flight mechanism's whole point is that its
state is about to change.** Writing the absence as a bare fact makes the
doc correct only until the mechanism lands — at which point every site that
said "doesn't exist yet" needs a second edit, and nothing marks those sites
as needing one.

**The fix that survives the landing, written once:** name both states in
the same sentence — *"today X; `#NNNN` will make it Y"* — rather than *"X,
because Y doesn't exist yet."* The first form is still true the moment
`#NNNN` lands (the doc now under-describes a shipped improvement instead of
asserting a false absence); the second form is falsified by the exact event
it was two paragraphs away from anticipating.

**Apply**: when a fix note or a doc explains the CURRENT state of a
mechanism that a linked, open issue is actively about to change, write the
future state alongside the current one, not as a separate follow-up. And
don't stop counting faces after the first re-grep confirms the fix you
already made — a claim repeated across N call sites was written by N
different authors at N different times; assume there's an (N+1)th until a
grep across the whole tree (§17: `src`/`tests`/`scripts`/`docs`, not just
the file you started with) comes back empty.

## 20. The enumeration hit and the classification missed — a correct population is not a correct verdict

2026-08-09/10, three sessions independently hit the same shape during the
same M4 test-migration arc, each getting the POPULATION right and the
PER-ITEM CLASSIFICATION wrong:

- **e2e-coder (#3976):** an AST scan surfaced a real call site; the session
  classified it as "correctly uses `default_sandbox_policy`" without
  reading that SAME call's other kwargs, which carried the actual defect.
  The hit was found; what it meant was not checked.
- **lead-coder (#3995→#4002):** the `__file__`-rooted-expression superset
  was enumerated correctly (every such expression in the tree), but the
  per-expression verdict used the wrong predicate ("does it leave its own
  directory") — misclassifying `Path(reyn.__file__)` (a different
  package's file, not a hazard) as dangerous, and `Path(__file__).parent /
  "_support"` (one hop, but `_support` doesn't travel with a moved file)
  as safe.
- **tui-coder (#4011):** a grep for `"tests/<name>.py"` string literals
  returned 30+ hits. A SAMPLE of a few were confirmed prose/docstring
  cross-references, and that verdict was extrapolated to the rest without
  checking each one — one of the un-sampled hits was actually a
  `_TCLI = "tests/…py"` registry constant, a programmatic reference, not
  prose.

**Why the pattern recurs together:** getting the population right is a
*procedural* improvement (switch grep to an AST scan, take the superset
instead of a hand-picked list) and it registers consciously as progress.
Classification has no equivalent shortcut — it can only be done one item
at a time — so "the count matches" or "the shape looks the same" becomes
tempting as a stopping point. The sample-then-extrapolate failure is
specifically MORE likely right after a population is correctly closed, not
less: the confidence earned from getting the harder procedural step right
transfers to the easier-feeling classification step, where it doesn't
belong. This is a different shape than §4's census-vs-structure gap (a
COUNT that turned out wrong) and different from §13's gate-scope gap (an
assertion that covers less than it claims) — here the set itself was
correct throughout, and the verdicts attached to its members were not.

**Apply:**
- After writing "the population is closed," ask "is the classification
  closed" as a SEPARATE question — the two are independent claims, and one
  being true says nothing about the other.
- If a verdict was reached by sampling N of M hits and extrapolating,
  write that down as sampling, not as a checked result. Written down, the
  next reader can challenge it; left implicit, it reads as measurement.
- Past the point where classifying each hit by hand doesn't scale, hand
  the classification to a MECHANISM, not a person re-reading faster — the
  `#4009` migration gate's own "a′" answer is exactly this: instead of
  inferring whether a moved `__file__` expression is now broken, it
  re-resolves the expression against the file's real post-move location
  and checks existence directly, no classification step at all.
- Every instance above was caught by something OTHER than the sweep that
  produced the miscount: CI (#3989/#3994), `ruff`'s `I001` surfacing an
  unusual import (#4011's dotted-form gap), or the referenced side's own
  test suite (#4011's registry constant). An audit does not reliably find
  its own blind spot — plan for a second, independent mechanism to catch
  what the first one's classification step missed, rather than trusting a
  repeated pass by the same method.

## 21. The search's shape decides the population — and a predicate can leave a gap no item falls into

§20 is about a population that was closed and verdicts that were not. This
section is the step before: the population itself was never whole, because
the *form* of the search — not its scope, not its spelling — excluded a class
of item silently. Four independent mechanisms hit this shape (three on one
night, 2026-08-09/10; a fourth on 2026-08-11), each in a different form, and
**no two of them would have been caught by fixing the other**.

- **A two-branch predicate with a gap between the branches (#4019).** The
  `b` gate's `_references_a_fixed_tests_location()` asked
  `if target.parent == tests_dir` — the target must be a *direct child* of
  `tests/`. For `tests/fixtures/llm/fp0063_arc_witness`, `.parent` is
  `tests/fixtures/llm`, so branch (b) was False; branch (a) ("outside
  `tests/`") was also False, because it is inside. The issue's own summary:
  「**穴の 形は «`.parent` 単発」でも «多段 join» でも ありません** ——
  **«tests-root peer の «中» に 何階層か 入った» という 深さです。**
  `tests/fixtures` 直下なら 捕まり、`tests/fixtures/llm/...` だと 抜ける」.
  The item was not misclassified — **it received no verdict at all**, because
  the two branches did not partition the space.

- **A population taken from the moving side (#4025).** The `core` bucket
  audit enumerated 「移動する 274 件」. The reference that broke lived in
  `tests/builtin/` — a directory that was **not moving** (it had already
  migrated in #4003): a `_REPO_ROOT / "tests" / "test_workspace_glob_outside_root_perm.py"`
  existence assert pointing at a file this PR moved away. The reviewer's
  correction: 「**«移動する ファイルの 中の 参照» を いくら 数えても この 1 件は
  出ません。正しい 母集団は «移動する 集合を 参照している 全ファイル» —— 向きが 逆**」.
  Scope was not the problem; **reading every moving file, without limit, never
  produces this item.**

- **A literal that must begin where the pattern expects (#4006).** The census
  grep required the quote to be immediately followed by `tests/`, so a string
  such as `"REDded tests/test_X.py::…"` — the path appearing mid-literal —
  never matched. Re-measured: 「従来 知られていた 母集団 17 件 / 取り直した
  母集団 287 件」. This one is §3/§4's token-spelling shape and §17's
  census-scope shape wearing a third face, and is included here only because
  it fired the same night through a third mechanism.

- **Two lists, each independently incomplete, cross-checked and STILL wrong
  once (#4337, 2026-08-11).** A `.reyn/config/` reference doc listed 5
  files; the runtime's own `_HOT_RELOAD_FILES` listed 6. Neither list was
  wrong by scope or by spelling — each was simply missing entries the OTHER
  list happened to have (the doc lacked `skills.yaml`/`pipelines.yaml`/
  `presentations.yaml`; the runtime list lacked `integrations.yaml`/
  `index/sources.yaml`, by design — a hot-reload IN-set is a narrower
  question than "everything in config/"). Cross-referencing the two got to
  8. Re-measured against the actual reader/writer code for EACH candidate
  (not the doc, not the runtime list — the real installer call sites), the
  true count was 7 wired + 1 (`integrations.yaml`) that had never been read
  or written by anything, anywhere — a THIRD number, different from both
  starting lists and from their union. Two incomplete populations, unioned,
  is not automatically the true population; each one can be missing
  something the other is ALSO missing.

**Why these are four axes and not one.** Widening the file-set (§17) fixes
the third and none of the other three. Fixing the predicate's branches fixes
the first alone. Reversing the direction fixes the second alone. Grounding
BOTH sides in the real underlying code (not just cross-referencing them
against each other) fixes the fourth alone — union-of-two-lists silently
inherited whatever both lists agreed to omit. They share a single sentence —
*the form of the search decided which items could ever appear* — and share
no remedy.

⚠️ **The author of this section committed the second one while writing the
surrounding work.** The #4021 link-gate specification, written the same night,
defined its population as "links **from** `deep-dives/{decisions,proposals,
contributing,spec}`" — outgoing only. Links pointing **into** those directories
(50 of them, from the built docs) were left unguarded, and the omission was
found by a peer's pin rather than by re-reading the spec. Having just written
"time-tense decides which directories are in scope," the direction axis was
dropped in the same paragraph: **finding one axis feels like having found the
axis.**

### What actually closed them

Not a checklist. All four were caught by a mechanism performing the real
operation and observing the result:

- #4019 — a **pre-move audit that actually moved files**, not a predicate about
  what a move would do
- #4025 — **CI's own pytest run** after the move landed
- #4006 — a **re-measurement triggered by #4025's red**, not by re-reading the
  original grep
- #4337 — **reading the actual reader/writer code for each candidate**, not
  cross-referencing the two lists against each other one more time

This is §20's last bullet at population scope: the sweep does not find its own
blind spot. It is also why `a′` (#4009) is the shape to copy — it **re-resolves
the expression against the file's real post-move location** instead of
classifying whether a move would break it. A predicate reasons about a
population it has already narrowed; performing the operation cannot narrow what
it has not yet touched.

**Apply**:

- For any move or rename, take **two** populations and say so: ① what the
  moving set references, ② **what references the moving set**. ②'s search runs
  in the opposite direction — build the list of names being moved and match it
  against string literals across the whole repo; a `__file__`-resolution scan
  cannot produce it.
- When a gate's predicate has branches, ask what falls into **neither**. Two
  conditions that both return False are indistinguishable from a clean item;
  a predicate that partitions ("inside its own home, at any depth" vs "not")
  has no such gap, while one built from two positive tests does.
- State the population's **form** alongside its size, the way §17 asks for
  scope: "287, matched by `<pattern>`, over `<file-set>`, in the `<direction>`
  direction." A bare count carries none of the three.

## 22. CI-green is not main-green when a test reads process-global state — the reverse direction from `pytest-green ≠ CI-green`

CLAUDE.md's PR-workflow section already names one direction of this: a
scoped `pytest` run passing locally is not the same claim as CI passing
(`ruff` and Tier-4 gates can fail while `pytest` itself is green). This
section is the **reverse** direction, found the same night (#4395/#4421):
**a test passing when run as part of the FULL suite is not the same claim
as that test passing on its own** — and the gap runs the opposite way from
what the name suggests. It is not "the full suite catches more"; it is
that **running the full suite can make an individually-broken test look
green**, because some OTHER test, earlier in file/collection order,
happens to satisfy a precondition this test never states.

**The shape:** `litellm_bootstrap.py`'s `ensure_litellm_ready()` exposes
`is_litellm_ready()` — a process-global flag, True only once THIS
module's own chokepoint has genuinely imported litellm. Two existing test
files (`test_llm_call_retry.py`, `test_3905_cli_authentication_error_
boundary.py`) each did their own direct `import litellm` /
`from litellm.exceptions import X` at the top — a reasonable-looking way
to get a real exception instance to test against — which puts litellm in
`sys.modules` but never touches `is_litellm_ready()`'s own bookkeeping.
Functions gated on `is_litellm_ready()` (`_is_retryable_exc`,
`_is_llm_timeout_exc`, the CLI's `AuthenticationError` boundary) silently
under-classified in these tests as a result — not a crash, a wrong,
overly-conservative answer that happened to still satisfy some of the
assertions.

**Why the full suite hid it:** in a full-repo `pytest` run, dozens of
OTHER test files call the real chokepoint before these two files' tests
ever execute, leaving `is_litellm_ready()` True by the time collection
reaches them — the precondition these two files never state gets
satisfied by accident, from outside the file, by whatever ran first. Run
either file **alone** (`pytest tests/llm/test_llm_call_retry.py`, no
other file in the invocation) and the same tests go red — the process
starts fresh, nothing upstream has touched the chokepoint yet, and the
gap is no longer hidden by borrowed state.

**The detection technique:** run the SPECIFIC file(s) touched by a change
in isolation, not folded into a broader scoped run — a passing scoped
`pytest tests/llm/` can still hide a single file that only passes because
of what ran before it in that same invocation. `git stash` the source
change and re-run the SAME isolated file to confirm the failure is real
and not an artifact of the stash itself (the technique this repo already
uses to distinguish "was already broken" from "I just broke it" —
confirmed here that the 4 failures predated this PR, on the currently
merged `main`, not introduced by it).

**The general form:** any test that depends on process-global state (a
readiness flag, a warm cache, a singleton's own "have I run yet" bit)
without EITHER establishing that state itself OR asserting it as an
explicit precondition is riding on collection order — and a full-suite
run's order is exactly the kind of incidental, unstated dependency this
repo's own testing policy already bans for other reasons (Tier 4:
"internal cache structure, exact... order"). The fix is the same shape
every time: state the real precondition explicitly in the test (call the
real chokepoint, or reset the relevant global in an autouse fixture)
instead of relying on some other file having already done it first.

## See also

- [Testing policy](testing.md) — Tier model, Mock vs Fake, decision flow.
- [CLAUDE.md](../../../CLAUDE.md) — the doc-sync hard rule (a doc
  describing a mechanism goes stale the moment the mechanism changes) is the
  same family: a claim whose referent moved out from under it.
