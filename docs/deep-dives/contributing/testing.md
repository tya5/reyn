# Testing Policy

Reyn aims for **predictability over autonomy** (see Project vision (principles doc removed)). Its test suite reflects that aim: **tests must guard the invariants that protect the OS, and must not become a tax on future evolution.**

This document is the policy. New tests should pass through the [decision flow](#decision-flow) before being written, and existing tests not consistent with the policy will be refactored or removed when next touched.

---

## Core principle

> A good test is judged by what it *signals when it breaks*. "Hard to break" is not a virtue — that property belongs to the design (P1–P8) and to the OS, not to the test.

If a test cannot articulate which contract or invariant it protects, it is implementation pinning in disguise. Implementation pinning is the most common cause of test rot: the test breaks every time the implementation evolves, the author updates it without re-evaluating its purpose, and the suite slowly becomes a friction layer rather than a feedback layer.

---

## Tier model

Tests in `tests/` belong to exactly one tier. The tier determines what the test pins, who the audience is, and when the test should change.

### The prerequisite every tier shares

**Owner ruling (2026-08-09).** This was implicit in every tier below since
the day this document was written, but never stated as its own requirement
— which is how a test could carry a Tier label without ever being checked
against it. 79% of this repo's test functions currently declare `Tier 2`;
that number describes how many tests *typed the label*, not how many meet
what the label requires. Quoted verbatim, because the point of writing this
down is to stop the requirement from travelling by word of mouth:

> 「OS 不変条件は振る舞いと契約が前提だよ」
> ("An OS invariant presupposes a behavior and a contract.")
>
> 「必要なのは振る舞いと契約」
> ("What's required is a behavior or a contract.")
>
> 「こじつけた理由しかないなら削除すべき。Tier4 じゃないという判断自体も疑うべき」
> ("If the only reason is contrived, delete it. Even the judgment that it
> isn't Tier 4 should be doubted.")

Claiming any tier — 1, 2, or 3 — presupposes two things, in order:

1. **You can name, in one line, the behavior or contract this test
   protects.** Not "it's reyn's" — reyn's own trivia can be named in one
   line too, and fits no tier. Not the implementation ("this function is
   written this way") and not a past bug's fingerprint ("X used to happen").
2. **What you named exists somewhere other than this test's own
   docstring.** A doc page, one of the [charter](../../concepts/architecture/charter.md)'s
   eight lenses, a decision record (an issue or PR body), or a promise made
   to a user. A reason that lives only inside the test that's supposed to
   be justified by it is not an anchor — it's the test citing itself.

An issue or PR number passes ② by *shape* whether or not it passes it in
substance — "an issue exists" is not "an issue decided a behavior." A
decision record is only a real anchor when what it decided is independent
of the test that cites it:

> **Discriminator: delete the test. Does the anchor's sentence go false?**
> No, it still holds → the anchor describes reyn, independent of this test
> → valid. Yes, it goes false → the anchor was describing *the test*, not
> reyn → circular, not an anchor.

The shape most likely to pass ② without meeting it: **the issue or PR that
exists *because* this test was going to be written** — its body argues for
adding the test, which is the same content as the test's own docstring,
just filed in a different place. Most likely of all: **the PR that landed
the test itself** — citing it as the anchor is, almost by construction,
circular.

```
✓ "the overlay stays up and readable when the pool is fully dead"   — behavior
✓ "an audit-event's type is a closed vocabulary"                     — contract
✓ issue #2074 body: "unify capability narrowing" (decides a behavior,
  independent of any one test)
✗ "a TTE effect resolves to its input"                — a third party's promise
✗ "bug X used to happen here"                          — a past bug's fingerprint
✗ "this function is written this way"                  — implementation, transcribed
✗ "PR #1234 body (names this exact test)"     — the PR that landed the test
✗ "PR #1234 body: rationale for adding this test" — argues for the test,
  same content as its docstring, filed elsewhere — still circular
```

**A test that fails this prerequisite was never classified as Tier 4 —
it never had standing to claim a tier at all.** Tier 4 (below) is a
considered judgment: this *shape* of test is recognized and excluded on
purpose. Failing to name an anchor is different — there is nothing yet to
judge. Say the shape is Tier 4 only once you can name what's missing;
otherwise the honest state is "not yet classified."

**Tier 2 (OS invariant) is where this bites hardest**, because the
category's own name reads like permission: almost any assertion about OS
behavior can be described as "an invariant." But an invariant is, by
definition, something the OS has promised to always uphold — which means
it already *is* a contract. If you cannot point to where that promise
exists outside the test, what you have is not an invariant, and the test
is not Tier 2 regardless of what its docstring's first line says.

**This is not a new axis alongside `CLAUDE.md`'s "Test review — six
questions"** — it's a different question, asked before those six. The six
questions ask whether a test that already claims a tier is *well-formed*
(does the assert survive a dead mechanism, is it a transcription, does it
accumulate unboundedly, …). This prerequisite asks whether the test had
any business claiming a tier in the first place. A test can pass all six
quality questions and still fail this one — a well-formed assertion about
something nobody promised is still pinning nothing real.

**The mechanism does not check this today.** `scripts/test_tier_audit.py`
matches a test's declared Tier line as a *string*
(`^Tier [123][abc]?:`, case-insensitive) — a docstring that types "Tier 2:"
passes the audit whether or not the behavior or contract it names exists
anywhere outside that docstring. Naming an anchor and having one are
different claims, and only a human reviewer checks the second.

### Tier 1 — Contract

**Pins**: external boundaries that users / OSS contributors / integration scripts depend on.

- `reyn.yaml` schema (required fields, types, error cases)
- Events JSONL payload schemas (audit and replay tooling depend on these)
- DSL contracts: required sections of `skill.md`, `phase.md`, `artifact.yaml`
- Public Python API surface re-exported from each cluster's `__init__.py`

**Granularity**: schema-level. Specific wording is not pinned, except for error message tokens that users grep for (e.g. an exception class name, a config key name).

**Pending**: CLI output formatting is **not** Tier 1 in the current revision. CLI UX is being reworked; CLI output contracts will be added after the redesign.

### Tier 2 — OS invariant

**Pins**: invariants of the OS architecture and its subsystems.

Two sub-categories:

- **Tier 2a — Core principle invariants** (P1–P8 directly):
  - LLM output contract (`type=transition` ⇒ `next_phase` non-null; `type=finish` ⇒ `next_phase` null)
  - **P1**: a phase that includes its own output schema is rejected by the OS
  - **P5**: data passed between phases outside the workspace channel is not honored as input to the next phase
  - **P6**: state mutations that bypass the events log are detected (= every state mutation produces an event)

- **Tier 2b — Subsystem invariants**: derived contracts of major subsystems
  (resume / persistence / dispatch / scheduling). Examples:
  - "WAL `step_completed` event lets resume memoize without re-execution"
  - "Restored intervention's answer routes to the resuming skill"
  - "BudgetTracker survives crash via `save_state` / `load_state`"
  - "Schema version mismatch refuses load with `--reset` hint"

  The contract is real and worth pinning, even though it's not a direct
  P1–P8 derivation.

- **Tier 2c — Multi-component integration (e2e)**: a single test exercises
  several modules to verify end-to-end behavior of an invariant. Uses
  real instances throughout; LLM is faked via a stub real callable —
  `@pytest.mark.llm_stub` (see [LLMStub — the second Fake](#llmstub-the-second-fake)
  below), NOT via `LLMReplay` (that path is Tier 3). **The LLM is the only
  collaborator that may be faked.** Replacing a non-LLM collaborator
  (e.g. a backend, a launcher, an external service) with a fake
  exercises only the caller's logic — the collaborator's own construction
  and behavior remain unverified. Integration of such collaborators is
  proven only by tests that run the real thing. Examples:
  - "Crash mid-skill → restart → resume → completes" (`test_resume_e2e.py`)
  - "Schema mismatch → CLI exits cleanly with `--reset` hint"
  - "BudgetTracker cap enforced across crash + restart"

**Granularity**: invariants. The test must fail when the invariant is violated, regardless of *how* it was violated.

**Target count**: 1–2 cases per invariant. Total grows organically with the
number of subsystems and integration points worth pinning. Do not interpret
this as a license to dump implementation tests — the [decision flow](#decision-flow)
filters those out.

### Tier 3 — LLM-replay tests (deterministic, fake LLM)

**Pins**: behavior of LLM-dependent OS paths, exercised through the
`LLMReplay` Fake at the `litellm.acompletion` boundary. **Mocks are
forbidden — see [Mock vs Fake](#mock-vs-fake) below.**

Note on terminology: a Tier 3 test specifically uses `LLMReplay` (recorded
fixture replay against the real `litellm` API surface). End-to-end
integration tests that fake the LLM via a simpler stub callable
(`@pytest.mark.llm_stub` — see [LLMStub — the second Fake](#llmstub-the-second-fake)
below) belong in **Tier 2c** above, not Tier 3.

#### Tier 3a — Single-call replay (current scope)

One LLM call per test, one phase. Canonical example: "Given this `ContextFrame`, the router classifies the intent as X". Drift detection is mandatory: each area also has a test that intentionally diverges and asserts `MissingFixture` is raised.

Areas covered today:
- `skill_router` — intent classification (1–2 typical, 1 drift)
- `multi_hop` — chain_id propagation, deferred reply (1 typical, 1 drift)
- `skill_improver` — temp-copy workflow + force_decide (1 typical, 1 drift)
- `eval_builder` — per-case criteria, rollback-loop case (1 typical, 1 drift)

**Target count**: 6–8 cases total across all areas (cap is 4 areas × 2 cases). 12+ cases is a sign of redundant corner-case coverage that belongs in Tier 4 (don't write).

#### Tier 3b — End-to-end scenario replay (deferred)

Multi-phase sessions, asserting on final state of workspace + events store. Currently **out of scope**: depends on the CLI / `ChatSession` driver, which is being reworked. To be added after the CLI redesign.

### Tier 4 — Don't write

Tests that fall in this list are **not** added to the suite, even when they would technically pass:

- **Direct assertions on private state** (`tracker._daily_tokens == 100`). Use `snapshot()` / public API instead.
- **Setup that mutates private state to bypass real flow** (e.g.
  `session._buffered[key] = value` to pre-populate a cache that the
  test then queries). If reaching the desired state via public API is
  too expensive, the API surface should expose what's needed — the
  test should not bypass. Setup-via-private is the same anti-pattern as
  assert-on-private; it just shifts the brittleness from the assertion
  to the setup phase.
- **Internal coordination flags** (`_state_loaded`, `_initialized`,
  `_cache_dirty`). Test the resulting *behavior*, not the flag.
  Implementation-detail flags are by definition internal; pinning them
  freezes the design.
- **Algorithm pinning** (sort order, dict iteration order, internal cache structure)
- **Per-commit regression duplicates**. The fix is the commit; the description in the PR is the record. Don't add a test for "this specific bug" unless it represents a genuine invariant that should hold forever.
- **LLM output quality / semantic correctness** ("is this answer useful?"). This belongs to the `eval` skill (LLM-as-judge), not the test suite — see [Out of policy](#out-of-policy).
- **Cosmetic format pins** (whitespace, punctuation, line counts, colour codes)
- **Snapshot / golden file tests** — see [Why no snapshot tests](#why-no-snapshot-tests). Narrow exception in the [Annex](#annex-scaffolding-tests).
- **`unittest.mock` patches of `litellm`** — use the [Fake](#mock-vs-fake) (`LLMReplay`) path instead.
- **Coverage targets** (e.g. "≥ 80% line coverage"). Coverage is a side-effect, not a goal. We do not gate PRs on it.
- **TDD by default**. Test-first is appropriate for Tier 2 invariants (where the contract is clear before the implementation). For feature work, "make it work, then guard it" is preferred — premature tests freeze designs that haven't been validated.
- **A third party's promise, tested as if it were reyn's** — see [Third-party promises are not reyn's to test](#third-party-promises-are-not-reyns-to-test) below.

### Third-party promises are not reyn's to test

**Owner ruling (2026-08-09).** The [prerequisite above](#the-prerequisite-every-tier-shares)
already excludes a third party's property from the anchor examples (a TTE
effect resolving to its input is TTE's promise, not reyn's) — but that was
written down as one example, not as a discriminator, and a rule that only
exists as an example doesn't transfer: the same shape recurred in the
sandbox suite (kernel-level SBPL/Landlock deny enforcement) without being
recognized as the same case, because nothing generalized "TTE" to "any
dependency reyn doesn't own."

> **外部ライブラリの機能テストを reyn に入れるべきなの？**
> ("Should a third-party library's own functionality be tested inside
> reyn?")

**Discriminator: if this assert fails, whose bug is it?**

- Apple's sandbox profile compiler, the Linux kernel, a pinned library →
  **third-party**. reyn does not carry this test.
- reyn's own code → reyn's, write it.

**Two boundary shapes get conflated as "one thing" — they aren't:**

```
✗ THE OTHER SIDE'S PROMISE
  "the kernel enforces the SBPL/Landlock deny"
  "reading ~/.ssh is actually refused"
    ← reyn's own contract is "~/.ssh is in the default policy" —
      and a string/structural test already verifies that in CI

✓ REYN'S OWN USE OF IT
  "reyn's policy actually reaches the enforcement point and doesn't fail
   silently"
  "the deny leg fires on the real backend and fails on the Noop backend"
    ← a dead wire looks identical to "denied" on either backend; this is
      reyn's own self-diagnosis, not the kernel's behavior
  "a backend that enforces write but ignores allow_subprocess passes
   Stage 1" (#3017)
    ← names what reyn's own self-test cannot witness
```

**The practical tell, before deciding either way**: look for the twin.

```
🔴 If a CI-running test already asserts the same claim at the
   string/structural level, its kernel-level twin is almost always
   THIRD-PARTY — the string-level test is what actually verifies reyn's
   own contract, and the kernel-level version is re-testing the kernel.
```

**Industry framing** (the caveat below matters as much as the rule): don't
test a dependency's own behavior — wrap it in an adapter and test the
adapter (8th Light, "Unit Testing Code Boundaries" / "Don't Mock What You
Don't Own"); don't test a framework's internals, the standard library, or
a third-party library's behavior (TestRail). The caveat every source
attaches: still write the integration test for *whether reyn is using the
dependency correctly* — that's the ✓ column above, not excluded by this
rule.

**The discriminator has three answers, not two.** "Delete" and "keep" are
not the only outcomes — a test can fail the discriminator (the value it
reads IS a third party's) while still protecting something real that
belongs to reyn, because the assert is checking the wrong thing to say so.

```
assert tabs_bar.region.height == 2
```

The discriminator says third-party (Textual's own default Tabs height,
not reyn's), and grepping reyn's layout code for the literal `2` finds
nothing either — by the rule above, this looks like CONTRIVED. But a
CSS comment next to the widget records a real incident (#3311): a
`height: auto` was mistakenly added, expanding the tabs bar to the
whole ~30-line TTY in production; `widget-state` assertions didn't
catch it, which is why the test reads `Widget.region` directly. **Delete
would remove a witness for a real regression reyn already had once.**

The fix is neither delete nor keep as-is — **rewrite what it names**:

```
✗ "Textual's Tabs defaults to height 2"        — a third party's default
✓ "reyn does not impose a height override on the tabs bar" — reyn's own
  promise, anchored at #3311, and true regardless of what Textual's own
  default happens to be
```

Reading a third party's value is not automatically CONTRIVED — the value
being read and the contract being protected are different questions.
Before deleting a test whose discriminator answer is "third-party," spend
one look at what it was written to prevent (docstring, an adjacent
comment, the anchor issue's body). If that was a reyn regression, the
contract is reyn's; only the assert's current *shape* pins the wrong
side of the boundary.

---

## Decision flow

Before writing a test, answer these questions. Each of Q1's tier answers
still needs [the prerequisite above](#the-prerequisite-every-tier-shares)
satisfied — naming a tier here is not itself the anchor.

```
Q1. If this breaks, who notices?
  A. External user / integrator              → Tier 1 (CLI output is currently deferred)
  B. The OS itself (an invariant fails)      → Tier 2
  C. A single LLM call drifts                → Tier 3a
  D. A whole session drifts                  → Tier 3b (deferred — wait for CLI redesign)
  E. Only the author of this commit          → don't write — PR description is enough

Q2. Will this become a friction in future work?
  - Pins a shape that skill changes will touch          → don't write
  - Pins a private name that refactor will rename       → don't write
  - Pins behavior the DSL is expected to extend         → don't write

Q3. At what level does it pin?
  - Public contract / OS invariant level                → write
  - Implementation level                                → don't write

Q4. Are you measuring LLM semantic quality?
  → Out of test-suite scope. Use the `eval` skill (LLM-as-judge).
    Reference: Anthropic's "regression eval" vs "capability eval" split.
```

When a test cannot be placed cleanly in Tier 1–3, it almost always belongs in Tier 4.

---

## Verification approach: static replay vs real-env run

How to verify a feature depends on whether its behavior is deterministic or variance-gated:

- **Deterministic mechanism** — a pure function, a parsing rule, an OS invariant with a fixed output for a fixed input: use a **static replay** (feed captured input against a gold expectation). The test is fully reproducible. For LLM-dependent paths, `LLMReplay` is the static replay mechanism (see [Tier 3](#tier-3--llm-replay-tests-deterministic-fake-llm)).
- **Variance-gated behavior** — LLM routing, probability-dependent triggers, or any feature that fires only under certain conditions: use **N≥3 real-env runs that each actually fire the behavior under test**. A single smoke run cannot distinguish "working" from "happened to pass once." The measurement is whether the behavior fires in each run, not whether the pipeline completes without error.

Counting failures across pipeline re-runs without establishing a zero-floor baseline is noise for variance-gated paths. Fix structural causes (missing context, schema mismatches, incorrect wiring) until the behavior fires reliably; only then do remaining failures reflect genuine model-capability limits.

---

## Time

**Owner ruling (2026-08-06), no exceptions: a test carries no time limit of
its own.** Not `@pytest.mark.timeout(N)`, not a wait budget written into the
test body (`attempts=200`, `range(150)`, a fixed retry count). When a test
needs to wait, it waits on the CONDITION, unboundedly — pass or fail never
depends on how much time elapsed, only on whether the condition it's
actually testing became true. A straight-line `sleep(N)` — using elapsed
time itself as the thing that makes an assertion pass — is banned outright,
the same way it always was; this section states the discipline explicitly
because this repo's own violations show the ban was never actually
enforced, not because the rule itself is new. Summarized as a Key
constraints bullet in `CLAUDE.md` too — update both if this changes.

**CI's `--timeout=120` is a kill switch, not a per-test contract.** It
exists to bound the damage a single hung test does to the rest of the run —
nothing more. Hitting it is never read as "ran on a slow environment"; it
means one of two things, and the investigation starts by finding out
which: the test should be decomposed (it's doing more than one thing should
have to wait for), or something actually hung. **The kill does not promise
cleanup** — a killed test's fixtures and teardown are not guaranteed to
run, and under `-n auto` an entire worker process can go down with it — so
nothing may treat the kill switch as a safety net to lean on.

**The discriminator, when it's unclear whether a wait is timing or
conditioning**: multiply whatever constant is in the test by 10. If the
test's pass/fail outcome changes, the test was timing, not waiting on a
condition — the constant was load-bearing for the *answer*, not just for
how long the test takes to get there.

### Why no exception survived design

Three attempts at carving out a sanctioned exception were each proposed and
each fell for the same underlying reason, which is why this section states
a rule with no exception clause rather than a taxonomy:

1. *"Don't write assertions that depend on real time."* Rejected — a
   discriminator that depends on the author already believing they've done
   the forbidden thing doesn't fire. The test that actually caused this
   policy to be revisited (#3746) froze an injected clock and said so in
   its own diff; the author genuinely believed the assertion was
   time-independent. It wasn't — the widget under test armed its timer
   through the framework's own real wall-clock `set_timer`, a SECOND time
   source the frozen clock never touched. A rule aimed at self-recognized
   violations cannot catch a violation the author didn't recognize as one.
2. *"Bless bounded polling as the sanctioned idiom"* (an attempt loop with
   a fixed retry count, e.g. `attempts=200, delay=0.01`). Rejected — the
   passing side of a bounded-poll loop is genuinely time-independent (it
   returns the moment the condition holds), but the FAILING side isn't: the
   `200` is an arbitrary constant nobody has agreed to, measured on
   whatever machine first got it to pass. Hang protection for that failing
   side already exists — the CI kill switch — so a per-test attempt budget
   is a degraded, silent duplicate of a mechanism that already exists and
   already logs why it fired.
3. *"Declare a legitimately-slow test's budget explicitly, via a marker, as
   a contract."* Rejected on the same ground as (2) before it was even
   tried: any such marker's value is still a constant measured in one
   environment, with no witness that it holds in another — declaring it
   doesn't make it true elsewhere. (It also turned out to be unimplementable
   as proposed: `@pytest.mark.timeout(N, reason=...)` was suggested before
   checking that `pytest-timeout` actually accepts a `reason` argument — it
   doesn't, and passing one raises `TypeError`.)

**Every attempt at a sanctioned exception needed an environment-dependent
constant to express it.** That isn't three failed designs; it's the same
failure recurring, and it's the actual argument for having no exception
clause at all — a rule that can only be stated using a number measured
somewhere is a rule that is false somewhere else.

### The general form of #3746's real defect

`sleep` was not the defect in #3746 — the bounded poll that used it was a
legitimate, condition-respecting wait, and would time out cleanly through
the CI kill switch if the condition it waited on genuinely never became
true. The real defect was that the test's assertion depended on a clock it
had NOT verified was the only relevant time source: production armed a
timer against the real wall clock while the test had frozen a different,
injected one. Generalized: **a test that injects or freezes a clock must
establish that the assertion under test depends on that clock alone** —
not merely that the clock was frozen, which is a claim about what the
AUTHOR did, not about what the SYSTEM under test actually reads from.

### Decomposing a slow test

"No time limit" is not "wait however long it takes and call it fine" — a
test whose upper layer is slow ONLY because a lower layer it depends on is
genuinely slow should be decomposed: replace the slow lower layer with a
Fake at the upper layer's boundary, so the upper layer's own logic can be
asserted without waiting on the lower layer's real latency. See
[When a Fake is justified](#when-a-fake-is-justified--and-what-it-requires)
below — a Fake used for this purpose carries the same contract-test
obligation as any other Fake in this doc, not a lighter one.

## Mock vs Fake

LLM-dependent tests **must** use the Fake (`LLMReplay`). Mocks are forbidden.
**This ban is not litellm-specific — it applies to every collaborator a test
constructs, callables and plain data/state objects alike** (see
[Faking a data/state object](#faking-a-datastate-object-same-ban-sharper-failure-mode)
below); litellm/`LLMReplay` is simply where the repo's normative example lives.

### LLMStub — the second Fake

Two Fakes exist at the LLM boundary, not one. `LLMReplay`
(`@pytest.mark.replay(path)`) answers "what did the model actually say,
byte for byte" from a recorded fixture — the Tier 3 mechanism above.
`LLMStub` (`@pytest.mark.llm_stub`, #5103) answers a narrower question
some tests never needed a fixture to ask: "did the real turn machinery
actually run" — the Tier 2c stub-callable mechanism named above.

**Difference that matters most**: `LLMStub` reads and writes NO fixture
file at all — no fixture key to construct for it, and by construction it
is invisible to both #3662's `MissingFixture` safety net and #5283's
unconsumed-entry check (there is nothing on disk for either to see).
`LLMReplay` does both of those things; `LLMStub` does neither.

**When to reach for it**: the subject under test is loop/valve/lifecycle/
wiring behavior around a turn, not the model's own output. If the test
needs to assert on what the model said, use `LLMReplay` (Tier 3) instead.

**Tier rule**: a test using `@pytest.mark.llm_stub` must NOT declare
Tier 3 — `test_tier_audit.py` enforces this pairing. The point of this
Fake is precisely that the completion's own content is not the subject
under test.

`LLMStub`'s own module docstring (`reyn.dev.testing.llm_stub`) is the
SSoT for its current modes (as of this writing: a cause-injection mode
and a gating mode) — not reproduced here, so this page does not go
stale the next time a mode is added.

### Why

A mock replaces the function with a hand-written stub:

```python
# FORBIDDEN
from unittest.mock import patch
with patch("litellm.acompletion", return_value=hand_built_dict):
    ...
```

This bypasses the real API contract. When `litellm` changes its signature or response shape (e.g. when LangChain renamed `__call__` to `invoke()`, mocked tests across the ecosystem continued to pass while production was broken — see Lincoln Loop, "Avoiding Mocks: Testing LLM Applications with LangChain in Django"), mocked tests do not detect it.

A fake routes through the real API surface. `LLMReplay` patches `litellm.acompletion`, but reconstructs a real `litellm.ModelResponse` from recorded data. Signature drift is detected at the call site (TypeError, AttributeError) or at lookup time (`MissingFixture`).

**Two distinct failure modes, not one.** A faked *callable* (a function/`__call__`) bypasses signature-drift detection — a real call would raise loudly (`TypeError`) when the contract changes; a mock just silently returns whatever it was told to. A faked *data/state object* (a hand-rolled stand-in passed as a collaborator, not invoked) has a worse failure mode: it can silently carry a field the real type doesn't have, and reading a nonexistent field via `getattr(obj, "field", default)` **raises nothing at all** — it just returns the default, forever, with no signature to drift and no call to fail. If the real collaborator is cheaply constructible (a plain dataclass, no I/O), build the real one — there is no cost trade-off that justifies faking it.

#### `monkeypatch.setattr` with a real callable — allowed

pytest's `monkeypatch.setattr(target, real_callable)` is **acceptable**
when the replacement is a real callable: a function defined in the test,
a concrete class with `__call__`, or another already-existing function.
The hazard the policy targets is `MagicMock`/`AsyncMock` returning
auto-spec'd Mock objects that bypass the real signature/contract.

Example (allowed):

```python
class _ScriptedLLM:
    def __init__(self, script):
        self.script = script
        self.calls = 0
    async def __call__(self, model, frame, *args, **kwargs):
        self.calls += 1
        return LLMCallResult(data=self.script[self.calls - 1], usage=None)

monkeypatch.setattr(runtime_mod, "call_llm", _ScriptedLLM(script))
```

The replacement here is a real class with a typed `__call__`; signature
drift in `call_llm` raises `TypeError` at invocation, just like a real
implementation would. **This allowance is for callables only** — it does
not extend to hand-rolling a hand-shaped stand-in for a plain data/state
object; see below.

#### Faking a data/state object: same ban, sharper failure mode

```python
# FORBIDDEN
class FakeRouterCallerState:
    def __init__(self):
        self.permission_resolver = _AllowAllResolver()  # invented — the real
                                                          # RouterCallerState has no such field
```

`#3037`: a hand-rolled fake for `RouterCallerState` invented a
`permission_resolver` field the real dataclass does not have. The gate under
test read that field via `getattr(state, "permission_resolver", None)`,
found the fake's invented value, and reported CLEAR — the gate had never
once executed against a real `RouterCallerState` and could not have, since
the real type has no such field. In production this let an LLM write
`.reyn/config/mcp.yaml` with no permission gate at all. This is the sharper
failure mode described above: no signature to violate, no call to fail —
just a silently-wrong default, forever. **If the real collaborator is a
plain dataclass, construct it for real; there is no test-isolation reason to
hand-roll a stand-in for something that costs nothing to build.**

For LLM-dependent paths, prefer `LLMReplay` (Tier 3) — it preserves the
full litellm boundary. Stub callables are appropriate for Tier 2c
integration tests where the LLM is incidental rather than under test.

#### A test seam must not weaken a construction-wiring gate

When a class is guarded by a structural / AST invariant gate — say a gate that
walks the AST for `node.func.id == "RouterLoop"` to prove every construction site
wires a required keyword — a Tier-2 test seam for that class must **not** replace
the construction. Avoid a *factory* seam (`RouterLoop(...)` redirected through an
injected `_loop_factory(...)`): it deletes the literal constructor call from the
AST, so the gate finds zero sites and fails, and it reopens the very hole the gate
protects — the injected callable can omit the guarded kwargs. Use a
**post-construction observer** instead — a spy callback handed the already-built
instance:

```python
# the class still constructs itself the real way; the observer only inspects it
loop = RouterLoop(..., resume_always_on=...)   # literal call stays in the AST
if self._loop_observer is not None:
    self._loop_observer(loop)                  # spy on the resolved instance
```

The observer keeps the literal construction in the AST (the gate still sees it)
and lets the real constructor wire the guarded kwargs unconditionally; the test
asserts on the resolved instance (`loop.router_model`, …) rather than raw
constructor kwargs. It is the seam analogue of the rule above: adapt to the
contract, don't bypass it.

### The strip-falsify mimicry

Two independent sessions, one hour apart, wrote the same shape (`#3902`'s
`_NoFoldEventLog`, `#3916`'s `_PreNineOhOneAgentLayer`):

1. Subclass the production class; override the method under test with a
   hand-written body that breaks the one branch being checked.
2. Assert the subclass behaves the way it was just written to behave.
3. Call it a strip-falsify — a proof the mechanism is load-bearing.

It proves nothing: the production method never runs. Deleting the real
mechanism entirely leaves this test exactly as green as it started, because
the test was never exercising the real thing — it was exercising a stand-in
whose behavior the test's own author dictated one line above the assert.
Both authors even wrote the disclaimer the [Mock vs Fake](#mock-vs-fake)
section asks for — `#3902`: *"not a mock of EventLog, a genuine (if
deliberately broken) instance"*; `#3916`: *"not a mock, a genuine instance
sharing every other method"* — and both are correct: neither is a mock.
**That's exactly why this recurs.** The Fake ban's own reason (a mock's
signature can drift silently from the real API) doesn't apply to a real
subclass with one overridden method, so avoiding a mock reads as clearing
the bar. It doesn't — the bar these tests miss is different: does the test
ever run the production code it claims to falsify? A genuine instance that
never calls the real method under test fails that bar exactly like a mock
does, for an unrelated reason a mock's own ban never named.

**"Does the test run the production code" is necessary but not
sufficient — the shape recurs even through a `super()` call.** A subclass
can call `super()` (so the real method genuinely executes) and still
commit this anti-pattern, if what gets asserted is the subclass's own
post-processing of that call, not anything about what production actually
did. The real discriminator is narrower: **is the hand-built stub the
*target of the assert*, or only an *input condition* feeding a real
production check?**

```
✗ calls super(), then mangles the result, then asserts the mangled value
  — the assert examines the stub's own arithmetic, production ran but
  wasn't checked
✓ never calls super() at all, but only as an input a real production
  function is asked to classify — the assert examines what PRODUCTION
  concluded, not what the stub did
```

`#3917`'s own census turned up a legitimate instance of the second shape:
`_WholesaleDead` in `test_sandbox_self_test_2983.py` is a `NoopBackend`
subclass whose `wrap_command` hand-breaks the `allow_subprocess=False`
branch (the real #2962 shape — a filter that kills the target command
outright). The test that uses it does not assert anything about
`_WholesaleDead`'s own behavior; it asserts that reyn's real
`probe_subprocess_enforcement`, handed this backend, correctly reports it
as **not** enforcing — the stub is the input condition, production's own
classification is what's under test, and the docstring names the exact
production line the strip would kill (*"Strip the non-forking control
from `probe_subprocess_enforcement` and this test fails"*). Whether the
stub calls `super()` is a useful first screen (a stub that never calls
production obviously can't be an input condition to it) but not the rule
itself — a stub can call `super()` and still fail this bar if the assert
never asks production anything.

**Why "would it stay green with the mechanism dead?" invited this.** That
phrasing asks you to imagine a dead mechanism, and imagining one is
something you can always do *inside the test itself* — build a stub, break
one branch by hand, assert the stub does what you just wrote it to do. The
question CLAUDE.md's six questions now asks instead — **who would miss
this test if it were gone?** — cannot be answered by imagining anything: a
stub or a hand-assembled collaborator list is a configuration only the
test itself builds, so the honest answer to "who relies on this" is
nobody. Owner's framing (2026-08-09): a test review should not ask
questions execution already answers for free — "does this assert
currently fire" is exactly that, which is what made the file:line/RED
phrasing tried here first the wrong fix too: it still asked about
*execution*, only moved the record of it from the test to the PR body.
"Who would miss it" asks about *existence*, which review — not
execution — is the only thing that can answer.

**The conclusion, once you can name the shape: delete the test.** A test
whose only defensible witness is its own hand-built double is not a test
of the mechanism it claims to falsify — it is a test of the double, and
the double is not load-bearing to anyone. There is no version of this
shape worth keeping "if only it ran the real strip" — a strip-falsify that
actually exercises the mechanism does not need this shape at all (see
[the prerequisite every tier shares](#the-prerequisite-every-tier-shares):
a claim needs an anchor outside the test, and a stub the test built for
itself is not one).

### When a Fake is justified — and what it requires

Everything above states when a Fake is FORBIDDEN (the real collaborator is
cheaply constructible — build it). The positive case: when a real lower
layer is genuinely expensive to run — slow, not merely inconvenient — and
that cost would make an upper-layer test slow enough to need [decomposing](#decomposing-a-slow-test),
replace the lower layer with a Fake at the upper layer's boundary, so the
upper layer's own logic is asserted without paying the lower layer's real
cost.

**A Fake used this way carries an obligation the ban-side of this section
doesn't need to state separately: a contract test, independent of the
upper-layer test, asserting the Fake and the real object agree on
whatever the upper layer actually depends on.** Without a contract test,
a Fake substituted for cost reasons is just a cheaper way to buy green — it
is a claim that the real object behaves a certain way, never a witness
that it does. A Fake with no contract test is exactly the shape the
Mock ban above exists to prevent, wearing a different justification.

This is not a new mechanism to build — this repo already has one, in one
place, and the obligation here is to open it to every other substitution,
not invent a second form. `LLMReplay`'s [drift detection](#drift-detection--required-for-each-area)
already does exactly this: fixtures are recorded from the real API, and a
dedicated test intentionally constructs an input the fixture does not
cover, asserting `MissingFixture` is raised — proving the Fake fails
loudly the moment its behavior would diverge from what a real call
requires, rather than silently returning whatever it was told to. Any
other Fake introduced for cost/speed reasons needs the same shape: a real
recorded/derived behavior, and a test that fails when the Fake and the
real object would actually disagree.

### How

```python
@pytest.mark.replay("fixtures/llm/my_area/my_scenario.jsonl")
def test_my_phase():
    from reyn.testing.replay import REPLAY_DATETIME
    frame = ContextFrame(
        # ...
        current_datetime=REPLAY_DATETIME,  # required for stable keys
    )
    response = await call_llm(model, frame, ...)
    assert response.data["type"] == "decide"
```

See [How to write a replay test](#how-to-write-a-replay-test) below for the full setup.

---

## Why no snapshot tests

Snapshot tests pin the structural output of a phase / artifact / final result, then diff future runs against the snapshot. We **do not adopt them**. Reasons:

1. **They contradict P1.** Phase declares only `input_schema` and instructions; output shape is determined externally by the next phase's `input_schema` or by `final_output_schema`. A snapshot freezes that output shape inside a test, in tension with P1.
2. **Skill evolution breaks them.** Every skill modification touches artifacts, so snapshots are updated routinely. Routine snapshot updates devolve into "looks plausible, accept" — the snapshot stops being a guard.
3. **The diff review becomes vibe-checking.** Without an articulated invariant, "snapshot updated" reviews degrade into eyeballing. There is no principled way to tell "expected change" from "regression".
4. **Tier 2 (OS invariant) is the better tool.** What the snapshot tries to protect is usually some invariant about the LLM output structure or workspace state. Encode that invariant directly.

Industry literature aligns: see Coulman, *Snapshot Testing: Use With Care* (2016); Hughes, *Why Snapshot Testing Sucks*; the meta-analysis in *Snapshot Testing in Practice: Benefits and Drawbacks* (Science of Computer Programming, 2024).

A narrow exception exists in the [Annex](#annex-scaffolding-tests) for legacy refactor characterization, following Coulman's original framing.

---

## Choosing a negative example

Many gates need a value the system must **refuse**: an unregistered cell, an
unknown name, an unsupported kind. Choosing that value badly produces a test
that silently stops testing what it claims.

> **Take the negative example from OUTSIDE the space the system extends into.
> Use something that cannot ENTER the set, not something that merely is not in
> the set yet.**

The two look identical at the call site and behave completely differently over
time. `(retrieval, content_fence)` was never forbidden — it was a **legal**
combination that had not been implemented, in an arc (#3376) whose stated purpose
was to implement it. `"no-such-presentation"` is not a value of the presentation
axis at all, so no future work can register it.

Measured, not hypothesised (#3376): three tests pinned `(category,
content_fence)` as *the* unregistered cell. P2 registered it and all three went
RED; they were retargeted to `(retrieval, content_fence)`. P3 registered that
one, and the same three would have gone RED again. Each retarget looked like a
small fix and was really the same defect recurring.

**If an expiring witness is unavoidable**, declare its expiry inline in
falsifiable form (`comments.md` §4): name what registers it and what breaks when
that happens. A permanent witness is always preferred.

**Mark them.** A negative example is written exactly like a positive one, so
nothing can tell them apart by inspection — which is why a purely syntactic gate
for this is not possible. Importing the value from a shared, named module is the
mark; `tests/_support/tool_use_negative_examples.py` is the worked example, and
it is paired with an arm asserting the marked name really is off-axis. Marked
witnesses can be gated; unmarked ones cannot, and this section is then the only
thing standing behind them.

**Related failure**: making one site derive its negative examples from a registry
does **not** mean the value is nowhere hardcoded. After #3376 P1 derived the
unregistered set from the live registry, three other files still held the
literal. When you convert a site to a derivation, grep from the **value** side —
the literal, not the concept — across `tests/`, and count what you find before
deciding what to fix.

---

## Setup discipline

Reaching a particular state in a test sometimes requires a non-trivial
sequence of public-API calls. The temptation is to shortcut this by
mutating private state directly:

```python
# Tier 4 — DO NOT do this in setup
session._buffered_intervention_answers["run_id"] = some_answer
result = bus.request(iv)
assert result == some_answer
```

The test "works" but is brittle: any rename or refactor of
`_buffered_intervention_answers` breaks the test, even though the
public contract ("buffered answers from a previous run are returned by
bus.request") is unchanged.

The disciplined alternative is to populate the buffer via the real flow:

```python
# Acceptable — the buffer arrives via the real path
session.restore_state(snapshot_with_outstanding_intervention)
await session._maybe_answer_oldest_intervention("Charlie")
# now bus.request will see the buffered answer through the real flow
result = await bus.request(iv)
```

If the real-flow setup feels expensive, that is a *signal*, not a
problem to bypass. The signal often points to a missing public method
on the subject (e.g. `BudgetTracker.is_state_loaded()` instead of
`_state_loaded`) or to a subsystem that needs better integration
fixtures.

Rule of thumb: if the test's setup section uses `_` private attributes
to inject state, it's pinning implementation. Refactor.

## Annex: Scaffolding tests

This is the only place tests with bounded life are allowed. **Scaffolding is not a Tier** — it is intentionally framed as a special-case exception so the `tests/` suite as a whole stays principled.

### When

You are about to do a substantial refactor or migration of an existing area, and you want to catch unintended behavior changes during the work. A scaffolding test pins the current behavior, lives only as long as the refactor, and is removed when the refactor is done.

### Required metadata

```python
# scaffold: triggered_by="When BudgetLedger is replaced with a different backing store"
# scaffold: removed_by="The PR that lands the new backing store"
def test_ledger_jsonl_format_during_migration():
    ...
```

The trigger must be **observable**. "When this code path is rewritten" is fine; "when we have time" or "after Q4" is not.

### Removal hygiene

The PR that fires the trigger event **must also remove the scaffolding tests in the same PR**. PR review checks for this.

### Physical isolation

Scaffolding tests live in `tests/scaffold/`. Files under that directory are scanned during PR review for stale triggers (whose triggering event has already happened).

### Snapshot test exception

A snapshot test is permitted **only** as scaffolding for legacy refactor (Coulman's "characterization test" use case). It must:
- live in `tests/scaffold/`,
- have a concrete `triggered_by` (the refactor PR or release),
- be removed when the refactor lands.

This is the only sanctioned use of snapshot tests in the codebase.

---

## Where a new test file goes

`tests/<name>/` bucket placement is enforced by `scripts/check_tests_dir_names.py`
(a CI gate — declared, not just described) but the *reasoning* behind that gate's
rules lives only in its own docstring, which nobody reads before writing a test.
This section is the human-readable form; #3879's bucket-reorganization arc
(2026-08-09/10) is where each rule below was settled, several after a wrong
first attempt corrected in the same thread.

**Split by subject, not by mirroring `src/reyn/`.** Mirroring the real package
tree (`tests/<name>/` for a real `src/reyn/<name>/`) is the *default* shape and
covers most buckets, but it is not the only valid one — `tests/chat`/`tests/cli`/
`tests/web` are pre-#3879 buckets that share a name with an unrelated
`src/reyn/<name>/` package without actually testing it (grandfathered, not a
template to extend), and `tests/repo/` is a deliberate, permanent special case
for AST-guard/CI-structure tests that import zero `reyn.*` at all — there is no
`src/reyn/repo/` and never will be. A same-night correction: a first attempt at
this same bucket used the invented name `tests/structure/` before checking
whether an existing, already-reserved name (`repo`) already covered the exact
use case — grep the existing vocabulary before inventing a new word.

**A new NON-MIRROR bucket name requires editing the gate, not just moving
files.** `check_tests_dir_names.py`'s rule ① is a closed set: a real
`src/reyn/<name>/` package, or the literal `repo` special case — nothing else
passes. When a genuinely new subject (e.g. `tests/intervention/` for
`user_intervention.py`/`intervention_choices.py`, both top-level single-file
modules with no `src/reyn/intervention/` package to mirror) needs its own
bucket, the fix is adding that name to the gate's explicit exception list —
a deliberate, reviewed code change, not a baseline edit and not "rule ① now
means whatever seems reasonable." The alternative (loosen ① to "any subject
name is fine") was considered and rejected: it would make the vocabulary check
meaningless, since nothing would ever fail it again. An explicit-list edit
means a new non-mirror bucket only exists because someone deliberately added
it and said why — "silently accumulates" cannot happen to a list you have to
edit to grow.

**3 files justifies a bucket; 1–2 does not.** Measured against the smallest
existing buckets on `origin/main` (`observability`: 2, `chat`: 3, `scaffold`: 4)
— 3 is within precedent, not a new low bar. The line stays at "don't create a
bucket for 1–2 files"; 3 is where a bucket becomes worth the name.

**`__init__.py` presence/absence is not a placement axis.** It decides a
*different* question — the SHADOW check (same gate, same file): a
`tests/<name>/` directory that HAS an `__init__.py` becomes a regular Python
package, which can silently shadow a real top-level import if `<name>` collides
with something real code imports (confirmed directly: adding
`tests/scripts/__init__.py` broke two existing tests that `import scripts`).
Whether to bundle a set of test files together is a subject-matching decision;
whether the resulting directory needs (or must avoid) an `__init__.py` is a
completely separate, mechanical safety check on top of that decision — treating
"these files happen to share `__init__.py` status" as a reason to group them is
answering the wrong question.

**AST import-count is a starting hypothesis, not a verdict — read the file.**
An AST scan of which `reyn.*` symbol a test imports most is a fast first pass,
and it can still be wrong the same way a naive grep can: the winning import can
be a repeated INGREDIENT (a fake/helper class instantiated 2–3 times inside the
file) rather than the file's actual SUBJECT. Two files that "won" an
intervention-related import count on this exact night turned out, on reading
the module docstring and what the asserts actually check, to be about
permission-decl / JIT-ask logic — `user_intervention` types only supplied a
fake bus's vocabulary. Confirming the count against the file's own module
docstring and the subject of its assertions is not optional extra rigor; it is
the step that makes the count trustworthy.

**"Can be moved" is not "should be moved."** A file whose subject genuinely
fits a new or different bucket is still allowed to stay where a **prior**,
already-settled placement decision put it, if moving it would only be
justified by "it counted highest on one of several methods" rather than a
clearer reason. Flag the judgment call in the PR body (which bucket it could
also fit, and why it's staying) rather than resolving it silently — a
placement's own reasoning is worth recording even when the answer is "leave it."

---

## Filesystem isolation (= no real `~/.reyn/` pollution)

Tests **must not** modify the developer's real `~/.reyn/` files. The repository's `tests/conftest.py` already enforces this for the secret store via an autouse fixture that sets `REYN_SECRETS_PATH` to `tmp_path / "secrets.env"` for every test. As a result:

- `secrets.store.save_secret()` / `clear_secret()` / `load_secrets()` go to `tmp_path` automatically — no `monkeypatch.setattr` needed in individual tests.
- `reyn secret {set,list,clear,rotate}` CLI tests inherit the same isolation.

When adding new infra that touches user home (`~/.reyn/registry-cache/`, `~/.reyn/approvals.jsonl`, etc.), follow the same pattern:

1. Make the path resolver consult an env var (`REYN_*_PATH`) at call time, falling back to `Path.home() / ".reyn" / ...`.
2. Add an autouse fixture in `conftest.py` that points the env var at `tmp_path`.
3. Verify via md5: `~/.reyn/<file>` hash must be byte-identical before and after the test run.

Module-level constants (`_SECRETS_FILE = Path.home() / ".reyn" / "secrets.env"`) are evaluated at import time — `monkeypatch.setenv("HOME", ...)` after import has no effect. The env-var-at-call-time pattern avoids that footgun.

## Spawning a subprocess that imports `reyn` (= declare it)

**In-process, a test always reads the checkout it was started from** — `[tool.pytest.ini_options] pythonpath = ["src"]` puts `<rootdir>/src` on `sys.path`. **A subprocess gets no such favour**: it re-resolves `reyn` from the venv. In a git worktree — which has no venv of its own, and borrows the main checkout's — that answer is **whatever checkout the venv's editable `.pth` points at**, i.e. *someone else's working tree*. Both halves then go green while disagreeing, and the spawning half is the wrong one (#3024).

**If your test spawns anything that imports `reyn`, request `out_of_process_reyn` and pin what it returns:**

```python
def test_something(out_of_process_reyn):
    env = {**os.environ, "PYTHONPATH": out_of_process_reyn}
    subprocess.run([sys.executable, "-c", script], env=env)
```

The fixture derives the src root from the **in-process** `reyn` and verifies by measurement that a subprocess pinned to it reads that same `reyn` — so the test measures the tree under test, in a worktree as well as in CI.

**An MCP stdio server needs the pin threaded through the server's *configured* env**, not inherited: the MCP SDK passes a six-key whitelist (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`) that **drops `PYTHONPATH`**. See `tests/builtin/test_fp0063_p3_rag_pipelines.py`.

**If your test runs a `[project.scripts]` console script by name** (e.g. `reyn` — the builtin RAG MCP servers' `reyn-rag-chunker`/`reyn-rag-vector-store` console scripts were retired under ADR 0064 P5; a builtin `rag` plugin test launches its scripts directly via `<interpreter> <script path>` instead, see `tests/builtin/test_fp0063_p2_builtin_mcp_rag.py` / `tests/security/test_sandbox_seccomp_network_3030.py`), also request **`reyn_console_scripts`**. A venv installed before an entry point was declared does not carry that script, and the failure says neither "absent" nor "stale venv" — it surfaces as `execvp() failed` or, through a stdio client, `McpError: Connection closed`, both of which read as a broken feature. The fixture skips with the absent subject named, and **fails under CI** (where the venv installs this checkout, so an absence there is a CI-setup defect rather than a reason to go green).

To diagnose an environment directly — including outside pytest, e.g. a manual e2e or a co-vet run:

```bash
python scripts/verify_env_identity.py            # all checks
python scripts/verify_env_identity.py --only console-scripts
```

**Never re-install a stale venv by running `pip install -e .` from inside a worktree.** That repoints the shared venv's editable `.pth` at that worktree, and every other consumer of the venv silently starts reading it. Install from the checkout the venv is meant to serve.

## Prior art — the names for what this policy asks for

Reyn's testing rules were re-derived from scratch in conversation on 2026-08-15
before anyone searched for them. They are all established practice with names;
carrying the names is what lets a reader look the rest up instead of waiting for
someone to re-derive it (#4847).

| what our rule says | the established name | where to read it |
|---|---|---|
| Split the decision out as a pure function; keep the untestable part thin | **Humble Object** | [steven-giesel.com](https://steven-giesel.com/blogPost/47acad0a-255c-489b-a805-d0f46bde23e5/the-humble-object-pattern) · [xUnit Test Patterns ch.26 (Design-for-Testability)](https://www.oreilly.com/library/view/xunit-test-patterns/9780131495050/ch26.html) |
| Make the clock an input instead of sleeping | **Virtual Clock** (attributed to Perrotta — *attribution unverified, we do not hold the book*) | [xUnit Test Patterns ch.26](https://www.oreilly.com/library/view/xunit-test-patterns/9780131495050/ch26.html) |
| A test that waits on asyncio/the OS is testing someone else's function | *(our phrasing — the maxim "don't unit test third-party code" is widely repeated, but we found no canonical source to send a reader to)* | — |
| `MagicMock`/`patch` banned, `LLMReplay` allowed | the **test double** taxonomy — **five** kinds: dummy / stub / spy / mock / **fake** (Meszaros). `LLMReplay` is a *fake* (a working implementation taking a shortcut), which is why it is allowed where a *mock* is not | [xUnitPatterns — Mocks, Fakes, Stubs and Dummies](http://xunitpatterns.com/Mocks,%20Fakes,%20Stubs%20and%20Dummies.html) · [Fowler — Mocks Aren't Stubs](https://martinfowler.com/articles/mocksArentStubs.html) |

**Before hand-rolling a clock**, evaluate what already exists — and say in the PR
why it was not enough if you still hand-roll one:

- **`freezegun`** — its `real_asyncio` flag exists for exactly the failure this
  suite hit: the event loop keeps real monotonic time while `time.monotonic()`
  is frozen, so `asyncio.sleep()` does not break.
  ([repo](https://github.com/spulec/freezegun) · [comparison](https://betterstack.com/community/guides/testing/time-machine-vs-freezegun/))
- **`time-machine`** — C extension, patches at interpreter level; advances time
  in precise increments rather than only freezing it.
- **`sleepfake`** — context manager replacing `time.sleep` / `asyncio.sleep`.
  ([PyPI](https://pypi.org/project/sleepfake/))

**A collaborator that is cheap to construct can still be undrivable** — a watcher
whose only trigger is its own timer leaves a test that may neither fake it nor
wait for it. Give it an external drive (a `check()` the test calls); that is the
repair, not a fake and not a `sleep`.


## Out of policy

These belong outside the test suite:

- **LLM output semantic quality.** "Is this response actually useful?" is the `eval` skill's job (LLM-as-judge). The test suite asks "did the structure stay correct" — Anthropic calls this *regression eval*. Quality is *capability eval* and lives elsewhere.
- **Model-vs-model benchmarks** (gemini vs claude vs gpt). Use the `eval` skill or a dedicated benchmark tool.
- **Production traffic monitoring / alerts.** Use `events.jsonl` plus external monitoring; this is operational infrastructure, not a test.

---

## How to write a replay test

> Reference for Tier 3a tests, which are the most common contribution shape.

### Boilerplate

```python
import pytest
import asyncio
from reyn.llm.llm import call_llm
from reyn.schemas.models import ContextFrame
from reyn.testing.replay import REPLAY_DATETIME


@pytest.mark.replay("fixtures/llm/my_area/my_scenario.jsonl")
def test_my_phase_classifies_as_x():
    """Tier 3a: skill_router classifies a chitchat input as finish."""
    frame = ContextFrame(
        current_phase="classify",
        # ... other fields ...
        current_datetime=REPLAY_DATETIME,   # REQUIRED
    )

    result = asyncio.get_event_loop().run_until_complete(
        call_llm(
            model="gemini-2.5-flash-lite",
            frame=frame,
            prompt_cache_enabled=False,
            skill_name="skill_router",
            phase_role="chat_router",
        )
    )

    assert result.data["type"] == "decide"
    assert result.data["control"]["decision"] == "finish"
```

### Fixture path

Path is relative to `tests/`. E.g. `"fixtures/llm/skill_router/chitchat.jsonl"`.

### Recording fixtures

**First time** (fixture file does not exist): conftest detects this and switches to record mode automatically. You need a live LLM available (LiteLLM proxy at `localhost:4000` for local dev — see `project_local_env.md` in memory).

```bash
python -m pytest tests/test_replay_my_area.py -v
# Fixture written to tests/fixtures/llm/my_area/my_scenario.jsonl
```

**After intentional prompt drift**: re-record with `REYN_LLM_RECORD=1` —
deleting first is no longer required (#3634 made `LLMReplay.flush()` replace
a re-recorded/superseded entry instead of appending alongside it, so
regenerating in place cannot stack schema generations any more):

```bash
REYN_LLM_RECORD=1 python -m pytest tests/test_replay_my_area.py -v
```

Deleting first (`rm tests/fixtures/llm/my_area/my_scenario.jsonl`) still
works if you prefer an explicit "starting from nothing," but it is a
preference, not a correctness requirement — see `write-replay-tests.md`
Step 4 and ``reyn.dev.testing.replay.LLMReplay.flush``'s own docstring for
the mechanism, and `tests/dev/test_replay_fixture_no_stacking_3634.py` for the
CI gate that would catch it if a stacked fixture landed anyway.

### Drift detection — required for each area

Each Tier 3a area has one test that intentionally constructs a frame the fixture does not cover, asserting that `MissingFixture` is raised. This is the mechanism that catches accidental prompt drift.

```python
@pytest.mark.replay("fixtures/llm/my_area/my_scenario.jsonl")
def test_wrong_input_raises_missing_fixture():
    """Tier 3a: drift detection — changes to instructions / candidate_outputs
    must be reflected in re-recorded fixtures, otherwise the test fails loudly."""
    frame = ContextFrame(
        current_phase="classify",
        instructions="this is intentionally not in the fixture",
        current_datetime=REPLAY_DATETIME,
    )
    from reyn.testing.replay import MissingFixture
    with pytest.raises(MissingFixture):
        asyncio.get_event_loop().run_until_complete(call_llm(...))
```

### Fixture format

JSONL, one record per line:

```json
{"key": "<sha256>", "model": "gemini-2.5-flash-lite", "prompt_preview": "...", "response": {...}}
```

- `key` — `SHA256(model + canonical_json(messages))`
- `prompt_preview` — first 200 characters of the last message (grep aid)
- `response` — `litellm.ModelResponse.model_dump()`, reconstructed on replay

### Monkeypatch lifecycle

`tests/conftest.py` installs `LLMReplay` for tests with `@pytest.mark.replay` and restores in `try/finally`. Tests without the marker see real `litellm.acompletion`.

#4081: this section used to cite `test_no_monkeypatch_leak` in `tests/test_replay_skill_router.py` as the test that verifies the try/finally restore — that file was deleted in the #2435 skill/phase decouple (no successor; the deletion was a whole-subsystem removal, not a rename) and nothing replaced the specific test. The mechanism above is still live (read `tests/conftest.py`'s `_llm_replay` fixture to confirm), but no dedicated test currently pins the no-leak guarantee.

---

## Running tests

```bash
# All tests
python -m pytest tests/ -v

# Force record mode (live LLM required)
REYN_LLM_RECORD=1 python -m pytest tests/ -v
```

#4081: this block used to also show `python -m pytest tests/test_replay_*.py -v` and `python -m pytest tests/test_os_invariants.py -v` — both the glob and the literal path resolve to nothing today (the `tests/test_replay_skill_router.py` family and `tests/test_os_invariants.py` were removed in the #2435/#2438 skill/phase-engine bulk deletions, no successor). Dropped rather than replaced with an unverified new example — scope your own run to the files/keywords your change actually touches (see "Before you push" below).

---

## Before you push — the five CI gates

A green `pytest` run is **not** a green CI run. `.github/workflows/test.yml` runs
five *separate* gates. **Owner-approved (2026-08-07): run four of them locally,
plus whatever tests your diff actually touches — do not run the full suite
locally.** This is a reversal of #3750's same-day "four → five" fix, which made
this section say "run all five" for a few hours; if that history makes you want
to revert this back to five, read the next two paragraphs first.

**Why the full local run was dropped, not just discouraged.** The mechanism
first — `.github/workflows/test.yml`'s pytest job runs on a `matrix` of
`python-version: ["3.11", "3.12"]`, each on its own dedicated `ubuntu-latest`
runner: two clean machines, one per Python version, running concurrently, each
with `-n auto` claiming that machine's own cores for itself alone. Locally,
`-n auto` reads the SAME machine's core count, but that machine is very likely
also running other work at once (another session, another tool) — the same
flag means "use all the cores, they're yours" in CI and "take a share of
cores other processes are also using" locally. It is not the same operation
scaled down; it's the same command over a different, contended resource.

Measured under that contention: 900s for a clean local pass, with more than
one run killed partway through before finishing at all (load average 17 on
an 8-core machine, lead-coder's own measurement — plausible given multiple
concurrent `-n auto` sessions, not independently re-measured here). Separately
from the slowness, a local run — but never CI — reliably reports 6 failures
that are not about your change: `tests/llm/test_compaction_resolver_aware_1172.py`
and 5 others fail whenever `reyn.local.yaml` exists on the machine running
them. That file is gitignored, so it's present on most developer checkouts
and absent on CI and any fresh worktree (#3791) — the suite was reading
ambient developer config, and the polarity was backwards (CI green, the
configured developer red, always for a reason unrelated to their diff). CI
runs the same suite in a clean checkout in ~5 minutes across 11 checks — a
local full run is not merely slower than that, it is a WORSE measurement:
narrower (one Python version, not two, since running both locally means
paying the contention cost twice) and contaminated by config CI never sees.

**What this actually gives up, stated plainly**: a change that breaks a test
FAR from the files you touched is now something you learn from CI, not from
your own local run before pushing. That is a real cost, not a null one — the
judgment made here is that paying 900 contended, sometimes-wrong seconds on
every push to catch it slightly earlier is not worth it when CI reports the
same thing, correctly, in about 5 minutes. If that trade stops being worth it
(CI queue times grow, or the failure class this catches turns out to matter
more than expected), that's a reason to revisit this section, not a reason to
silently start running the full suite locally again without updating it.

1. **pytest, scoped to your diff** — run the tests your change actually
   touches, by file path or `-k <keyword>`, not the whole suite:
   ```bash
   python -m pytest tests/test_your_area.py -q
   # or, to catch anything matching a keyword across the tree:
   python -m pytest -q -k "your_feature"
   ```
   The `-n auto --timeout=120` flags CI uses exist to give full-suite
   collection parity and a hang-namer for a 10000+ test run — neither
   matters for a scoped local run of a handful of files; a plain invocation
   is fine. CI still runs the untruncated suite with those flags on every
   PR, so a hang in your change is still caught there even though you don't
   reproduce that flag combination locally.

   **A passing local sweep can still be missing files your local venv can't
   see** (#4104/#4101): CI installs every optional extra; a scoped local
   sweep runs whatever your own venv happens to have. A test gated on a
   missing extra (`pytest.importorskip`) never enters your local "N passed"
   count at all — it isn't a failure, it's invisible, wearing the same green
   a genuine pass does. `pytest_sessionfinish` (`src/reyn/dev/testing/extra_skip_report.py`,
   wired in `tests/conftest.py`) prints a separate, loud tally of exactly
   this at the end of any local run that hit one — no action needed to see
   it; it shows in the same terminal output your scoped run already
   produces, and stays silent in CI by construction (every extra is
   installed there, so there's nothing to report). **This only catches
   phrasing it recognizes** — it matches a skip reason containing "not
   installed" or "could not import" (`importorskip`'s own default and this
   repo's conventional phrasing), not "is this an optional-extra skip" in
   general; a hand-written `importorskip(..., reason="requires the foo
   extra")` slips past silently. Write new `importorskip` calls with a
   default or conventionally-phrased reason, or they won't get this net.
   **And this covers only `importorskip`-shaped skips** — a
   `@pytest.mark.skipif(...)` (a missing platform, a missing daemon like
   docker) doesn't go through `importorskip` at all, phrased well or not,
   and never enters the tally either. For those, six-question ④ ("would it
   stay green having never run") still needs a human answer — this
   mechanism narrows the population that question has to cover, it doesn't
   replace asking it.
2. **ruff** — lint + import-sort (`I001`):
   ```bash
   ruff check .        # add --fix for autofixable I001 / formatting
   ```
   The bare `.` is load-bearing, not shorthand: run the same scope CI runs
   (`test.yml`), not a narrower `src tests` subset — #4630 measured the gap
   left by the narrower command: `scripts/` and every other top-level
   directory went unchecked, and 17 genuinely-dead imports outside `src/`
   had been invisible to the whole checklist. A narrower local gate is a
   green that does not mean what it says.
3. **test-tier audit** — `scripts/test_tier_audit.py --strict` on each new or
   modified test file (the linter described under
   [Tier compliance auditor](#tier-compliance-auditor)). A Tier-4 format pin
   (`len(...) == N`, exact whitespace, line count) fails here even when pytest is
   green — replace it with a behavioural assertion (assert on the extracted value,
   not its length):
   ```bash
   python scripts/test_tier_audit.py --strict <changed_test_files>
   ```
4. **module-docstring gate** — `scripts/verify_module_docstrings.py` on each
   new or modified source file under `src/`. Fails when a module docstring
   contains narrative prose (implementation history, PR references, change log
   entries). On PR events CI scopes this to changed files only; run locally the
   same way:
   ```bash
   python scripts/verify_module_docstrings.py <changed_src_files>
   ```
5. **mypy ratchet** — `scripts/mypy_ratchet.py` (#3726). A *ratchet*, not full
   mypy adoption: it only fails on a `(file, error-code)` pair not already
   declared in `scripts/mypy_ratchet_baseline.json` — a genuinely new mypy
   finding in a file you touched fails this even though `mypy` itself isn't
   in this repo's mental model of "the linter":
   ```bash
   python scripts/mypy_ratchet.py
   ```
   ⚠️ It also fails when `mypy` is not importable by the interpreter running it
   (#4576) — that is a *precondition* failure, not a finding. Before the guard,
   a missing mypy produced `OK: 0 findings, all baselined (215 declared)` and
   exit 0: `python -m mypy` wrote "No module named mypy" to stderr, no `[code]`
   lines were parsed, and zero measured pairs minus the baseline is zero new
   pairs. The `215 declared` came from a baseline load that HAD succeeded, so
   the line looked fully alive. It hid a real `[call-arg]` through an entire
   local pre-PR check (#4575). If you installed reyn without the `dev` extra,
   this gate was never measuring anything for you.
6. **path-literal reference ratchet** — `scripts/check_tests_path_literal_reference.py`
   (#4065). Same ratchet shape as #5 against
   `scripts/check_tests_path_literal_reference_baseline.json`, but a different
   population: every `tests/...py` path literal repo-wide (docstring, comment,
   doc prose, YAML/workflow arg), checked against `git ls-files` — not the
   pytest collection, so a moved test's OLD path lingering in prose elsewhere
   fails here even though nothing executes that prose. Costs ~2.6s against
   mypy ratchet's ~76s (#4068 measurement, 2026-08-10) — cheap enough that cost
   is never the reason to skip it:
   ```bash
   python scripts/check_tests_path_literal_reference.py
   ```
   ⚠️ **This gate scans the whole repo, so its baseline goes stale the moment
   `main` moves underneath your branch** — a PR that both moves tests AND
   updates the baseline can pass locally, then fail in CI once a sibling PR's
   own move landed on `main` first. #4068 hit this 4 times in one night before
   the cause was identified (#3880). On any PR that moves or renames a
   `tests/...py` file, rebase onto latest `main` and run this gate again
   immediately before pushing — not once, earlier in the session.

A green scoped `pytest` run alone has shipped PRs that CI then bounced on ruff
(`I001`) or the tier audit (a `len(...) == 1` format pin). Report scope
honestly: say which of the six you actually ran locally (e.g. "ruff +
tier-audit + mypy ratchet + path-literal ratchet + the tests I touched")
rather than "suite passed" — that phrase implies the full local run this
section no longer asks for, and CI is the only place all six now run
together.

**A PR that touches `docs/` also owes a seventh, separate check — the
"docs build (strict)" CI job runs three steps, and all three must pass:**

```bash
mkdocs build --strict -f .mkdocs/mkdocs.yml && python scripts/check_doc_anchors.py && python scripts/check_retired_config_keys_denylist.py
```

Run the first two as a pair, in that order, not either alone. `mkdocs
build --strict` catches a dangling *file* reference but never checks
whether `#anchor` actually exists on the target page — that's
`check_doc_anchors.py`'s own job, checked against the `site/` the
mkdocs step just built (#3557/#3592: 42/42 line-number citations in
`charter.md` had drifted, silently, before this script existed).
Running `check_doc_anchors.py` alone, without a prior `mkdocs build`,
raises an `AssertionError` from a missing `site/` — which reads as
"main is broken," not as "run the other command first" (#4651: this
exact confusion, live, the same night a `git grep` first found this
script named in neither this file nor `CLAUDE.md`). The third,
`check_retired_config_keys_denylist.py` (#4327), is independent of the
first two — it just happens to live in the same CI job — and rejects a
retired top-level `reyn.yaml` key (renamed via #4174) still showing up
at YAML top level in operator-facing docs or `reyn.local.yaml.example`;
this doc's own first pass at documenting the job (this same PR's
earlier commit) stopped at 2 of its 3 steps, caught by #4651's own
follow-up measurement — the same under-scoping class the rest of this
section exists to close.

**If your PR touches `src/reyn/mcp/`, also run `python
scripts/check_fastmcp_import_boundary.py`** (#3698 enforcement half)
— a dedicated, path-filtered workflow
(`.github/workflows/fastmcp-import-boundary-gate.yml`, triggered only
on `src/reyn/mcp/**`, not part of `test.yml`'s own job list above).
Zero-baseline: no file under `src/reyn/mcp/` may `import fastmcp` at
all, since the last file that genuinely needed to
(`_fastmcp_boundary.py`) was retired clean-break (#4302).

**If your PR touches `tests/`, also run `python
scripts/check_bare_tests_import_reference.py` and `python
scripts/check_file_depth_reference.py`** (#4008 / #3995-#4002-#4019)
— two more dedicated, path-filtered workflows (both triggered on
`tests/**`, both whole-tree `tests_dir.rglob("*.py")` scans against a
baseline of zero, neither part of `test.yml`). The first rejects a
bare `from _some_module import x` import (no `tests.` prefix) in a
test file, which resolves today only because pytest's "prepend" import
mode happens to put a flat consumer's own directory on `sys.path`, and
silently breaks the moment that consumer moves into a subdirectory.
The second rejects a module-level `.glob(...)`/`.rglob(...)` call
whose root, resolved from the file's own current location, escapes
`tests/` or lands outside its current direct child directories — a
static, add-time proxy for "this path expression won't survive a
future move," not a check that anything has actually moved yet.

Separately, `scripts/flat_tests_ratchet.py` (#3879 Stage 0) is
**unconditional, not path-scoped like the checks above** — CI runs it
on every PR with no path filter (`.github/workflows/flat-tests-ratchet.yml`),
so it belongs alongside the six numbered gates rather than in this
conditional section; see `CLAUDE.md`'s "Before you open a PR" for the
command (`python scripts/flat_tests_ratchet.py`, no args needed
locally — `--check-growth` is CI-only, diffed against a base ref).

---

## Coverage checklist for a new OS feature

When adding a new LLM-dependent OS path:

- [ ] One Tier 3a test for the canonical happy path
- [ ] One Tier 3a test for one corner case (force_decide, error path, boundary)
- [ ] One drift detection test (`MissingFixture` assertion)
- [ ] If the feature derives from a P1–P8 invariant, add a Tier 2 test for it
- [ ] If the feature changes a public contract (yaml schema, events payload, DSL section), update / add a Tier 1 test
- [ ] Verify no `current_datetime=datetime.now()` — always `REPLAY_DATETIME`
- [ ] Each test has a one-line docstring naming its tier (e.g. `"""Tier 3a: ..."""`). The exact format is `Tier N:` or `Tier Na:` — any text between the tier designation and the colon fails the audit (e.g. `"""Tier 2 (MUST-1): ..."""` → fail; fix: `"""Tier 2: (MUST-1) ..."""`)

---

## Tier compliance auditor

An **automated linter** based on this policy: `scripts/test_tier_audit.py`.

Use it as a pre-commit check when adding new tests, for a Tier 4 violation sweep of the existing suite, or to audit test policy violations during PR review.

Detection rules (6):

- Missing or malformed Tier docstring (regex: `^Tier [123][abc]?:` — colon must follow directly)
- Format pinning (line count / char count / exact length = Tier 4 violation)
- Private state assertion
- MagicMock / AsyncMock / patch usage
- Bounded-life test in regular dir (scaffold/ candidate)
- Snapshot/golden test outside scaffold

Full reference: [docs/reference/test-tier-audit.md](../../reference/test-tier-audit.md)
