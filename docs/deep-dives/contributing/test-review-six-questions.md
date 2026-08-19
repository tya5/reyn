# Test review — the six questions, in full

Normative form (the six questions and the blocking table) lives in `CLAUDE.md`
§ "Test review". This file holds the instances each question was derived from,
the essays on ordering and accept-side tests, and the reviewer's-note history.

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
4. **The never-ran/nothing-to-bite-on question (full wording in `CLAUDE.md`).**
   skip / collection error / zero collected all wear green's
   colour. Name what a missing optional dependency silently skips — CI has no
   `effects` extra, so #3796's file skips whole and its green says nothing
   (#2999 is the same shape with a docker-daemon skip). The same green covers
   one step further in: a test that RAN, whose assert RAN, over an **empty**
   collection — `assert not [e for e in xs if …]` passes unconditionally when
   `xs` is empty, so the filter never decided anything (#4773: the author had
   written the `assert xs` guard; the reviewing pass ran the six questions and
   still didn't look, and an independent review found it). Count both the same
   way — a green path that says nothing, whether the test never ran or the
   assertion never had a subject.
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
So each question has a blocking answer, not just an answer — the table is in
`CLAUDE.md` § "Test review" (this file's own opening line already says so;
the table itself belongs there once, not twice — #4858 found this copy had
already drifted one word from that one).

⚠️ 4 blocks on the *silence*, not on the skip: a file that skips whole in CI is
often correct (an optional extra), and what makes it a defect is a green nobody
qualified. The empty-collection half reads the same way — a filter that finds
nothing is often the right answer, and what makes it a defect is a green that
never said whether the collection had anything in it. Naming it costs one
sentence; the fix, when it is one, is usually a guard the author already knows
how to write. 5 has no such carve-out — "it is small today" is a measurement of
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

**The "why did I have to touch this test" rule (verbatim in `CLAUDE.md`) has
its own instance here.** A
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
