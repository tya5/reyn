# Comment policy

This document is normative. New comments should pass through the
[classification](#2-classification-by-content-not-length) below before being
written, and existing comments not consistent with this policy are moved or
compressed when the code they sit on is next touched for another reason
(never as a drive-by sweep on its own).

---

## 1. Premise (remove this and everything below loses its meaning)

**A reyn code comment is a decision's storage location.** Not decoration, not
narration. Evidence from a single day (#3082 thread, three separate rulings
made from prose alone):

- **#3374**'s closing question — did an ambiguous test-vs-product finding need
  owner escalation? — was answered directly by `session.py:6059-6066`'s
  docstring: *"attempt a force-close wrap-up so the LLM can summarize what was
  accomplished before the turn ends … falls back to the original canned error
  + hardcoded reply if wrap-up fails or produces no text."* The intent was
  already on record; nobody needed to ask.
- **#3392**'s apparent schema mismatch — why does the retrieval scheme's
  `search_actions` tool definition not equal the registered `ToolDefinition`
  it advertises? — was settled by `retrieval.py::_search_tool_schema`'s own
  docstring: *"The **call** is intercepted by `interpret` → `RePresent`
  (never dispatched), so this only needs to advertise the search affordance
  to the LLM."* The mismatch is by design, not a defect.
- **#3376** turned the *absence* of a comment into the finding itself: an
  uncaptured cell in a tool-use oracle had no recorded justification, so its
  omission could not be distinguished from an oversight — the missing prose
  *was* the evidence that intent had not been established.

One day, three rulings, all from prose. Delete the comments and all three
become re-investigations.

## 2. Classification (by content, not length)

| Class | Content | Where it lives |
|---|---|---|
| **A** | History / measurement (evidence) | **Moves to a doc**; inline keeps a reference + the conclusion |
| **B** | A decision **+ a falsifiable reason** | **Stays inline** (may compress, but never drop the reason) |
| **C** | A **relational invariant** (a claim about two places) | **Stays inline in full, regardless of length** |
| **K-inline** | Why something was NOT done / a hypothesis that was measured and rejected / a measurement that justifies the current shape | **Never moves — fixed inline** |

**A, B, C, and K-inline are one axis — compressibility — not four
unrelated rules.** A is compressible to a reference + conclusion because its
content (history, measurement) has a natural home elsewhere that still
answers "why." C and K-inline are the two ways a comment can resist
compression, for opposite reasons: **C spans TWO locations** (compress it
into either one and the OTHER location loses the claim); **K-inline spans
ZERO locations** — it is a claim about code that does **not exist** (a path
not taken, a hypothesis already rejected), so there is no second site to
carry the claim to and nothing to attach a reference to. A residue (Class B)
sits on exactly one location and can compress freely because that is the
only place the claim needs to be legible.

**Do not classify by length.** A 7-line threshold was tried and rejected on
#3404: one reviewer counted "41 blocks / 414 lines," another counted "55 /
602" on the same file, and neither count was wrong — they used different,
equally defensible definitions of "a block." A membership test that changes
with how you count cannot be an acceptance gate. Demotions (a comment that
looks safe to move) are named explicitly, one at a time, never swept by a
size rule.

## 3. The `★` marker in source comments

The `★` is an editorial attention marker used in source comments. It is not
Python syntax, a runtime annotation, or a machine-readable severity level.
Its purpose is to tell a future reader that the surrounding claim deserves
careful reading because it records a decision, a measured boundary, a known
failure mode, or an explicit scope limit.

Use it in either of these forms:

- **Line-leading marker:** put `★` at the start of the comment text when the
  whole line carries the attention signal, for example `# ★ the event is
  client-authored` or a doc-comment equivalent.
- **Inline marker:** put `★` immediately before the particular sentence or
  clause that needs attention when the rest of the comment is ordinary
  explanation, for example `# the value is ★bounded by the caller`.

Place the marker next to the claim it qualifies. Do not use it as decoration,
as a substitute for explaining what breaks, or as a promise that a claim has
been independently measured. A marker does not change the comment's Class A,
B, C, or K-inline classification; classify the content first, then choose the
marker placement that keeps the claim's scope visible.

A full pass over `src/` found `★` in 44 files: 101 lines containing the
marker and 105 marker occurrences. The counts differ because 4 lines contain
more than one marker. For a reproducible shape count, this section uses
"line-leading" when the first non-comment character of the comment text is
`★`; all other occurrences are "inline". That pass classified 78 lines as
line-leading and 23 as inline. These are descriptive measurements, not a
migration target.

Existing source comments use both line-leading and inline forms. This is a
descriptive convention, not a migration target: do not rewrite existing
comments merely to normalize marker placement. When adding or editing a
comment, preserve the surrounding comment's form unless the new claim has a
clearer local placement. If the claim is historical or measured, state the
source and confidence separately; `★` alone is not evidence.

## 4. The test for Class C (one question, answerable by inspection)

> **Does this line assert something about a SECOND location? Does another
> symbol's / file's / field's correctness depend on what this line says?**

**Why only relational invariants survive compression, and why this is
structural, not a matter of degree**: a residue is a claim about *the line it
sits on* — compress it and it still describes that one line. A relational
invariant is a claim about **two places at once**, and is non-local wherever
you put it: move it to either location and it becomes a claim the OTHER
location can no longer see. Compression of a Class C comment does not fail
because someone wrote it badly — the claim is not expressible in fewer
places than two.

Known instance: `event_schema.py`'s `RETIRED_PHASE_FIELD` (#3355) — the field
is **required** by the persisted audit-event schema and **always empty**
because its producer (the phase engine) was deleted; both halves must be
visible in the same view, or a future reader sees only one half of a claim
that is false without the other.

Measured on `session.py` (#3404, 453 comment units — one unit per docstring or
per contiguous run of `#` lines classified as a whole, not a per-line count;
this is an observation from a classification pass, not a gate, so §2's
counting-disagreement problem does not apply to it): **C = 0 in this file** —
and that zero is trustworthy for a specific reason, not a default. The class
itself is not empty (`RETIRED_PHASE_FIELD` is a real, present member outside
this file); a "0 found" result is only credible when you can state the
condition that would make it non-zero, which the detection question above
lets you do. Never write "class C is empty" — write "class C is 0 *in this
file*, and here is what a positive instance would look like."

**A Class C comment must carry a witness or a timestamp** (#5371): every
other class has *something* that notices when it rots — Class A's evidence
still exists at its destination, Class B's reason gets read when its own
line is touched, K-inline is a claim about code that isn't there to drift.
Class C has none of that by construction (this section's own argument): the
claim lives at neither location's own review, so **it is the one class
structurally guaranteed not to go red when the other side changes** — a
present-tense guarantee with nothing backing it is a promise nobody is
still keeping. Carry one of:

- **(a) a witness** — name the test or gate that goes red if the relation
  breaks.
- **(b) a timestamp** — write it in the past tense, `as of #NNNN, X was Y`
  (architect), not as a standing present-tense guarantee, when the other
  side is somewhere you cannot attach a witness to at all (a third party's
  code, a different subsystem's own review). The claim is then a
  historical fact, not a promise — it cannot rot, because it never
  asserted anything about *now*.
- **(c) `until #NNNN`** — a variant of (b) for a gap that is known and
  tracked but not yet closed: name the issue whose landing would change
  what this line can claim, rather than leaving the reader to guess
  whether "not attempted" is a permanent design choice or a known TODO.

Known instances of (c), three real fixes from the same sweep (#5367②,
`router_history_buffer.py` / `engine.py` — each REPLACED an unqualified
present-tense claim the sweep found false):

> "a turn whose history is built more than once (a shrink-ladder retry, a
> re-send) re-enters this elide logic with no fresh `maybe_force_compact`
> run in between — nothing in this file, or the call site, re-triggers it."
>
> (Source line since removed, #5367 — the elide logic this comment named
> no longer exists. Kept here as a teaching example of the (c) pattern
> itself; the historical claim was true when written, and its own removal
> is the exact "the other side changed" case (b)/(c) exist to survive
> without becoming a lie in place.)

> "a turn whose body is a spillable tool result … could still be reduced
> without splitting it into more turns; this retry_loop does not attempt
> that today (tracked as #5367③)."

> "the turn-count shrink ladder attempted is not resolving this cause
> (content-level spill was not tried here)."

Each names the SPECIFIC gap the two-location claim depends on — a reader
knows exactly what "not attempted" covers, not just that something,
somewhere, might change.

**(d) An asserting docstring**: if the docstring names a specific witness
by identifier (a test function, a gate script), the SAME unit must contain
an `assert`/`pytest.raises`/equivalent that actually exercises it — naming
a witness you never call is a claim with the same structural gap this
section exists to close, one level down.

Only a present-tense claim with none of (a)–(c) can go stale without any
line change or test failure ever registering it — this is the one failure
mode unique to Class C among all four classes.

## 5. The shape of a residue (the load-bearing section)

**Write what BREAKS, never "do not change this."**

- ❌ `# do not raise this cap`
- ✅ `# cap=1: a 2nd handoff re-enters the same turn and double-counts budget (#1092; doc.md#anchor)`

**Why**: the instant the supporting evidence moves to a doc, an imperative
("do not change this") becomes an **unverifiable command** — a future reader
has no way to judge it or to tell when it has stopped applying. "Raising
this breaks X" can be evaluated on the spot, and refuted later if X stops
being true.

**Best when it names what ACTUALLY happened**, not merely what could happen.
The strongest instance from #3404: *"Sink closure resolves
`self._audit_events` at CALL time — eager `self.events` capture once
**silently disabled `search_actions` for every operator**."* It says the
break was **silent**, not merely possible — which is exactly what stops the
next person from "fixing" it back to eager capture.

## 6. Conditions for moving a comment to a doc

A comment may move to a doc only if BOTH hold:

1. **The inline reference names a section or anchor, not a heading's
   words** — `<doc>.md#<anchor>` form. `tests/scripts/test_3126_doc_anchor_gate.py`
   (#3126) verifies this exact form against the real slugify function that
   produces the doc's actual anchors, so a wrong or later-removed anchor
   fails CI immediately. **A reference by heading text is NOT gated** — it
   rots silently the next time the heading is reworded.
2. **The destination is the doc that owns the mechanism**, not a
   convenient-but-unrelated one. Moving evidence to the wrong doc means that
   when the mechanism changes, the doc won't be among the files the change's
   own PR touches — and CLAUDE.md's doc-sync hard rule (fix the doc in the
   SAME PR the mechanism changes) never fires, because nobody was looking at
   that doc.

**If either condition can't be met, do not move it** — downgrade to Class B
(compressed inline) or K-inline instead. When genuinely unsure, leave it
inline.

**A comment-only change is not gate-neutral.** Structural gates scan source
TEXT and do not distinguish a comment from code — a compressed comment that
happens to place a class name next to `(` can be misread as a construction
site. On #3404, rewording `#2421: route through the single MCPGateway seam`
to `route through MCPGateway (not a raw MCP client call)` tripped #2813's
completeness gate (`\bMCPGateway\s*\(` scanned for a nearby `cancel_event=`)
purely because of where the parenthesis landed — the original wording had
no `(` following the class name at all. When rewording a comment that names
a symbol, grep `tests/` for that symbol's regex first, or keep the symbol
away from the punctuation a gate keys on. (Swept for this specific risk on
#3404: only two such scanners exist repo-wide, `\bMCPClient\s*\(` and
`\bMCPGateway\s*\(` — a future sweep can start from that count rather than
re-deriving it.)

## 7. Do not set a numeric target

**Never set "reduce N lines" as a goal.** A line-count target creates
pressure to misclassify a Class C or K-inline comment as movable just to hit
the number — reduction must be a *consequence* of correct classification,
never a target it is measured against.

This was verified, not assumed (#3404): of the 301 inline blocks left
untouched, **86 needed no wording change at all** — they were already
correctly shaped. Comment *volume* was not where the bloat lived. That
negative finding — "most of what looked like bloat wasn't" — would not have
surfaced under a numeric target, because a target rewards moving things,
not correctly leaving them alone.

## 8. A "never do this in the future" comment owns its own update

A comment of the form *"do not make this change until X"* takes on an
obligation: when X actually happens, THAT change must update the comment
itself. Left alone, the prohibition survives after its reason has expired,
and a future reader sees a rule with no visible justification.

Correctly closed instance: `MIN_CONTRAST = 2.0`'s comment read *"raising
this to 3.0 is the start condition of #3371 — do not raise it here as a
drive-by."* #3401 (the PR that WAS #3371 landing) raised the value to `3.0`
**and rewrote that same comment in the same commit** to describe the new
value's own margin — the prohibition did not outlive its own trigger.

## 9. Rejected arguments (record these, or the same ground gets re-litigated)

The #3082 discussion converged by eliminating arguments, not by starting
from a blank page. Recording only the conclusion would let a future
participant re-derive — or worse, re-argue — the same four premises from
scratch:

- ❌ **"Inline is safe because it shows up in the same diff, so review catches
  staleness."** Falsified by measurement (#2884): the comment that went
  stale did so **inside the very file the falsifying PR itself edited** —
  same diff, same review pass, missed anyway. **What determines staleness is
  distance from the edit site, not which medium the words live in.**
- ❌ **"Doc round-trips are a cost, so prefer inline."** Not a valid
  argument for inline-by-default: a doc round-trip is a **selective** cost
  (only the reader who needs the evidence pays it — someone mid-investigation
  already has a question, so following a reference costs them nothing they
  weren't already going to spend); an inline block is an **unconditional**
  cost (every reader of that code pays it, every time, whether or not they
  ever had the question). Inline is for stopping a reader who has **no**
  question yet — a passer-by, not an investigator.
- ❌ **"Keep more in inline because inline degrades less than a doc."**
  Rejected: degradation is not a property specific to residues — docs
  degrade too, and faster, by the same #2884 measurement. This is the
  "inline resists drift better than docs" argument returning under a
  different name (caught mid-thread as exactly that re-introduction). The
  actual answer to degradation is already in §5: don't try to PREVENT it,
  make it DETECTABLE (a falsifiable "X breaks" claim goes stale visibly;
  "do not change this" does not).
- ❌ **"Keep important comments inline."** Not decidable as stated —
  importance depends on which question the reader currently holds, which
  this document cannot know in advance. Replaced entirely by §4's
  mechanical test: relational, or not.

---

## Sources

#3082 (classification and rulings) · #3404 (measurement: 453 units,
8017→7651 lines, comment density 18.1%→14.4%) · #2884 (staleness tracks
distance, not medium) · #3355 (Class C's known instance) · #3126 (anchor
gate) · #3371/#3401 (a prohibition comment updating itself) · #3374/#3392/#3376
(three same-day rulings made from prose alone).
