# PR workflow — full rationale

Normative rules live in `CLAUDE.md` § "PR workflow". This file holds the
reasoning, the measured instances, and the recovery angles behind each rule —
read it when a rule's shape is unclear or when proposing a change to one.

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

Two of those four names are **retired** (2026-08-24): `needs:owner-decision`
folded into `owner:decide`, `wait_owner_iv` into `owner:verify-only`. The
measurement above is unchanged — it was taken while all four existed, and the
two retired names were exact synonyms of the two that remain, not a separate
axis. What stays distinct is the axis itself: `owner:decide` (owner picks a
direction) and `owner:verify-only` (owner reproduces on their own machine) ask
for different acts, and `owner-hit` is a third thing again — the record that the
operator hit it in the shipped configuration, not a request for anything. Five
spellings for two acts is what produced the misreading this paragraph already
records.

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
   #N", "I'm picking up #M, please pause on it", and **delivering a NEW
   assignment** — no peer polls every issue, so an assignment written only
   on the issue reaches nobody (#4737 / #4763 / #4776; #4776 idled
   e2e-coder ~2h while its issue said "assigned"). **PR comments
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
   **This rule's checks all read the PR body — a `git commit` message and the
   PR's own title are two more SEPARATE surfaces the same keyword-plus-number
   matching applies to, and fixing either costs more.** `scripts/check_pr_closing_intent.py`
   scans every individual commit message in the PR too (not just the body),
   because a squash-merge's default commit message is the *concatenation* of
   the PR's own commit messages — a closing keyword sitting in any one of them
   appears in that default squash body regardless of what the PR body says
   (#3187: the PR body was clean; an intermediate commit's message wasn't;
   the squash-merge auto-closed the wrong issue). Editing the PR body does
   NOT fix a commit-message violation — the gate stays red until the
   offending commit is `amend`ed (or the history is rebased) and
   force-pushed, real extra cost a body edit never needs (#4443 hit this
   directly). **The PR title is scanned too, unconditionally** (#5321,
   corrected #5330): this repo's squash headline is built from the PR TITLE
   whenever a PR has 2+ commits, and a plain merge commit's body is the PR
   TITLE regardless of commit count — an earlier version gated the title
   scan on commit count and was wrong for the merge-commit case, so the
   check does not try to guess which merge method will be used. Editing a
   commit message does not fix a title violation either — each of the
   three surfaces (body, commit messages, title) needs its own fix. The
   avoidance is the same single rule everywhere: never place a closing
   keyword next to an issue number, in any of the three surfaces.
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
   pin, a comment's prose that never becomes a ticket). Filing the remainder
   is the start of its life as backlog, not a decision about its priority or
   its eventual close — both governed by
   `docs/deep-dives/contributing/issue-management.md`.
7. **A reviewer's blocking point goes in the PR body as an unchecked Test
   plan item, not only in a review comment.** `gh pr review --request-changes`
   does not work in this repo's setup: every session authenticates as the
   same `gh` user, and GitHub refuses to let an account request changes on
   its own pull request — there is no machine-readable `CHANGES_REQUESTED`
   state available here. A review comment alone used to be easy to miss
   because nothing about the PR's own checks-passing state reflected it:
   "all checks green" could be — and four times in one day was (#3720 ×2,
   #3722, #3730) — reported while a still-open review comment sat
   unaddressed, because the comment lived on a surface the merge decision
   didn't read. Since #5317, that gap is closed on the comment side too: a
   `BLOCKING (head <sha>)` comment reddens the PR's own checks on its own,
   with no body edit at all (`scripts/check_open_blocking_checkboxes.py`).
   This is **the reviewer's obligation**, not the implementer's: rule 1
   above already covers the implementer side (finish your OWN Test plan
   before merge). A reviewing session that has a blocking point edits the
   PR body to add it as `- [ ] 🔴 <point>` (append, don't remove the
   author's own items) — or posts the equivalent `BLOCKING (head <sha>)`
   comment form — and does not merge while it's unresolved. Ticking the
   box alone no longer closes it: closing takes a comment quoting that
   line verbatim (`BLOCKING-CLEARED (head <sha>)` for the comment form),
   or the gate stays red (#5314). The author reports the fix and asks the
   reviewer to confirm — the author does not tick the reviewer's own box.
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
8. **The TESTS-READ rule (`CLAUDE.md`'s own rule 8) has a gap instance
   here.** "A lead-coder merge train refuses
   any PR touching `tests/` without one" (Test review, above) describes
   what *that train's own script* does — it is not a rule that reaches a
   session merging directly. `#3916` (75 files, 31 under `tests/`) merged
   with no TESTS-READ ever posted: not blocked, not late — the gate
   the sentence implied was never wired to that path at all, the same
   declared-vs-actual gap this file has been naming all session, this
   time in its own PR-workflow section. The content was sound (confirmed
   post-hoc) and the author was not at fault — "resume" was said without
   the caveat that merge still waits on TESTS-READ, an omission on the
   reviewer's side, not a violation on the implementer's. At the time this
   was written: no CI gate — a check for "a comment containing a fixed
   string exists" makes passing the check the goal (an empty TESTS-READ
   satisfies it) — the same shape already closed elsewhere in this file. A
   doc line was the proportionate fix; only a human reading the PR before
   merging could tell a real TESTS-READ from an empty one.

   **Partially closed since (#5120, 2026-08-22):**
   `scripts/check_tests_read_names_its_tree.py`, wired as its own
   `pull_request`/`issue_comment` CI workflow (repo-wide, not one train's
   own script), now fails a `tests/`-touching PR that carries no
   TESTS-READ note naming one of the PR's own commits, or whose only note
   names a commit `tests/` has since moved past. This closes the
   "never wired to that path at all" half — every PR now gets a CI check,
   not just the ones a particular merge train happens to gate. It does
   NOT close the "empty TESTS-READ satisfies it" half this section
   illustrates: the gate reads only whether a note names a fresh commit,
   never what the note SAYS — `**[X]** — TESTS-READ (B) (head \`abc1234\`)`
   with no actual review content still passes. Only a human reading the
   PR can still tell a real TESTS-READ from an empty one carrying the
   right shape.

   **A B note claiming independence must say what only its author can
   say.** B's whole value is that it was reached without A's framing, so
   the note should state whether A had been read — but "I posted before
   A" is the wrong form: posting order is measurable, and on 2026-08-23 a
   B note claiming it was measured false (the A it claimed to precede was
   posted 57 seconds earlier). Whether the reviewer *read* A is knowable
   only to that reviewer, which is exactly why it has to be said rather
   than inferred. The form that carries information is "written without
   reading A (posted after it)". Either answer is fine; an unstated one
   leaves the reader unable to weigh the note, and a falsifiable one
   invites the reader to check the wrong thing.

   **Reporting reworked twice since (#5138):** a GitHub check run attaches
   to the sha its triggering event carries, and `issue_comment` (fired by
   a posted comment) carries the DEFAULT BRANCH's sha, not the PR's — a
   check run from that event could never land on the PR's own rollup.
   Measured 4/4 (#5127, #5128, #5132, #5136) stayed red until a human
   re-ran the workflow by hand. A first fix moved the claim line into the
   PR **body** (`pull_request: edited` carries the PR's own head, so a
   body edit's check run lands correctly) — retracted days later
   (architect, #5138 comment 5383200442) after it reproduced its own
   failure mode on #5144: the body is one document with many purposes
   (description, Test plan, blocking points), and that PR's body carried
   both TESTS-READ prose and, elsewhere in the same text, a real commit
   SHA — enough to pass with no reviewer note. The claim line moved back
   to a comment, but now only that comment's FIRST LINE is read (marker
   and head SHA co-located there; grounds from line 2 on), which excludes
   a document merely discussing TESTS-READ SYNTACTICALLY rather than
   statistically. Reporting moved off check runs entirely: the CI workflow
   posts a GitHub commit STATUS to the PR's own head sha (resolved via
   `gh pr view` regardless of which event triggered the run) — `pending`
   before the script runs, `success`/`failure` after, so a job that dies
   mid-run leaves the status `pending` (blocking merge) instead of going
   silent. One reporting channel, not a check run and a status both
   claiming the same fact.

   **Existential quantification closed to universal, then corrected back to
   existential over the CURRENT HEAD (#5197 → #5204, both 2026-08-23):**
   `evaluate` used to return green the instant ANY ONE note named a fresh
   commit of the PR — an existential check ("does a note naming the current
   tree exist"), where "a fresh commit" meant merely "not stale relative to
   `tests/`". Measured live on #5196: architect's A note named the new head;
   docs-maintainer's B note still named the PREVIOUS head, because a fresh
   witness commit (the fix for architect's own blocking finding) had landed
   to `tests/` in between — and the gate went green having read only the A
   note, since it stopped looking the moment it found one success. #5197's
   own fix over-corrected to a ∀ over every note that names ANY commit of
   the PR: reds the moment any note is stale relative to `tests/`, no matter
   how many other notes exist. That broke on #5201, live: a first B note
   went stale after a fix landed, the SAME reviewer posted a second,
   differential B note correctly naming the new head — and the PR stayed
   red FOREVER, because a comment thread only grows and the first note's own
   stale SHA never stops existing to fail the ∀. #5204 (architect,
   correcting #5197's own ruling) replaced the comparison target: the
   predicate is existential over the PR's CURRENT `headRefOid` specifically
   — "does some note name THIS commit" — not "any commit of the PR" (#5196's
   hole) and not "every note ever posted" (#5197's hole). An old note naming
   an old SHA simply doesn't match the (moving) target and is left exactly
   where it is, never edited or deleted; a single fresh note matching the
   current head is enough regardless of how many superseded notes sit above
   it. One behavior change worth naming: because the target is now the
   EXACT head rather than "no `tests/` commit landed since", a commit that
   moves the head without touching `tests/` (e.g. a docs-only fixup) still
   requires a fresh note — the pre-#5204 `tests/`-only leniency is gone.

   **This does not close, and was never meant to close, a second, separate
   limit:** the gate reads no role marker at all — it cannot tell an A note
   from a B note, or a B note from a third opinion, only that a comment's
   first line names the marker and the current head. #5187 gave a note's
   first line a role-disclosing shape parseable in principle, but adding a
   role parse here would make "A ran" a requirement this gate enforces, when
   only "B ran" is a house rule that exists — a new rule invented by the
   gate, not a reflection of one that does. Whether the RIGHT roles
   reviewed a PR remains a human's judgment reading the PR before merging,
   same as it always was; a green here means "some note names the exact
   tree about to merge," never "the tree was reviewed by the right people."

## Bundling and the owner's veto unit

A doc PR that exists to give the owner a veto opportunity should ship alone.
Bundling two such changes makes the revert unit one: the owner cannot reject
half of it without rejecting both. (#4813 bundled broker rule 3 with the TUI
colour-policy scope clarification; both were fine, but "veto rule 3 only" had
no cheap path.)
