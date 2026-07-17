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

## 1. The four faces of "observation ≠ referent"

| Face | What's missing | Instance |
|---|---|---|
| **Record is a lie** | The claim itself is false | `landlock.py` blamed network denial on "the no-network-fd / proxy gate" — a named mechanism that appears nowhere in the repo but that one comment (#3031). What actually denied `connect()` was a seccomp default-deny, itself skipped under `allow_subprocess=True` (#3030). |
| **Environment can't witness** | A green test never ran the risky path | The Landlock shim called `Ruleset` APIs (`add_path_beneath_rule` etc.) that don't exist in the pinned `landlock==1.0.0.dev5` — every call raised `AttributeError` in production for 41 days, while its own test called the shim's internals directly, bypassing the broken production entry point (#2980). |
| **Claim has no owner** | No one on the claimed subsystem's side checks it | Same `landlock.py` case: a doc/comment in subsystem A asserting subsystem B's behavior, with no owner on B's side to catch it wrong — "plausible and unowned" is why it survived. |
| **Observed-target identity unverified** | Green about the wrong object | Agent worktrees share the main checkout's `.venv` (0 of 136 have their own) — in-process and subprocess-imported `reyn` are two different trees "by construction, not staleness" (#3033). Separately: the same heading anchor resolves to two different slugs on GitHub vs mkdocs — "valid" is renderer-specific (#3039). |

**Apply**: before trusting a green result, name what it actually observed,
not what you're using it to conclude.

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

**Apply**: before trusting a "zero hits" result, ask whether the thing you're
checking for would leave a positive trace if present, or only an absence —
only the first kind makes zero a real answer.

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

## See also

- [Testing policy](testing.md) — Tier model, Mock vs Fake, decision flow.
- [CLAUDE.md](../../../../CLAUDE.md) — the doc-sync hard rule (a doc
  describing a mechanism goes stale the moment the mechanism changes) is the
  same family: a claim whose referent moved out from under it.
