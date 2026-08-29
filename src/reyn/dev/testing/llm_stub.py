"""LLMStub — fixture-less LLM-boundary stub for tests whose subject is the
loop/valve/lifecycle/wiring around a turn, not the model's own output (#5103).

Where ``LLMReplay`` (``@pytest.mark.replay(path)``) answers "what did the
model actually say, byte for byte" from a recorded fixture, ``LLMStub``
(``@pytest.mark.llm_stub``) answers a narrower question some tests never
needed a fixture to ask: "did the real turn machinery (``RouterLoopDriver``
/ ``RouterLoop`` / ``Session._run_router_loop``) actually run". Before this
existed, that question could only be asked by replacing
``Session._loop_driver.run_turn`` itself with a private, per-test stand-in
(a ``_noop``-shaped function) — which means the real ``run_turn`` never ran
at all, and nothing this stub now witnesses was ever exercised (#5103,
architect design "C2").

Design (architect ruling, #5103):
- A THIRD mode, not a wildcard fixture entry. A wildcard ``LLMReplay`` entry
  that answers every key was rejected (#5103 C1) — it would silently defeat
  ``MissingFixture``'s own #3662 safety net (a missing fixture no longer
  falls back to a real network call), and would poison #5283's
  unconsumed-entry check (a wildcard is *always* "consumed", so it can mask
  a genuinely stale sibling entry as live).
- ``LLMStub`` reads and writes no fixture file at all, so it is invisible
  to both of those mechanisms by construction — there is nothing for either
  to see.
- Every call gets the SAME minimal ``litellm.ModelResponse``: EXPLICITLY
  built with ``finish_reason="stop"``, ``tool_calls=None`` — the loop always
  sees "the model said nothing and asked for nothing", so it completes the
  turn without entering any tool-call branch. A test built for this stub
  must not assert on the completion's own content (that is what makes it
  not Tier 3 — see ``@pytest.mark.llm_stub``'s registration text below).
  #5103 TESTS-READ (architect): these two fields are explicitly SET here,
  not left to ``litellm.ModelResponse``'s own defaults — a third party's
  default is not this module's contract to inherit silently (if litellm's
  default ever changed, every ``@llm_stub`` test would silently start
  seeing something different, undetected, since the migrated tests assert
  on the LOOP side, not on the completion itself). See
  ``tests/dev/test_llm_stub_5103.py`` for the pinning test this finding
  asked for.

Only ``litellm.acompletion`` is patched (not ``aembedding``) — the turns
this stub exists for reach the completion boundary, never the embedding
one; a test that needs both should use ``@replay`` instead.

#5450 ``control=``: a SECOND, orthogonal axis (architect design, #5450)
for tests whose subject is CONTROLLING the LLM call, not merely avoiding
it — "呼ばれたら止まり、外から明示的に進められる" (called-then-hung,
externally released; e.g. #2242's hard-cancel-mid-generation tests).
Before this, those tests replaced ``Session._loop_driver.run_turn``
itself with a private controllable-hang closure — the SAME "手が届かな
かった" gap #5103 closed for the observation-only files: the test's OWN
docstring (``test_2242_hard_cancel.py``) already said the real
suspension point is "inside its ``litellm.acompletion`` await", so the
private replacement was standing in for a boundary this stub already
patches. ``control="gated"`` moves the hang/release to the REAL
boundary, so the real ``RouterLoopDriver``/``RouterLoop``/driver runs
for real (#5103's whole point), while still giving the test the same
external-release handle (``call_started``/``release``, the SAME 2
``asyncio.Event``s the original private helper returned).

Architect's design also specified a SECOND mode, ``"gated_swallow_
cancel"`` — a hang whose ``CancelledError`` is caught and swallowed,
simulating a third party (litellm/httpx) absorbing a cancellation
instead of propagating it, to test whether reyn's own ``finally``
still resets state under that hypothetical. NOT implemented: architect's
own explicit precondition for keeping it ("実装の1手目に、litellm/httpx
が実際に握り潰し得るかを実行で確かめてください — 起こし得ないなら B は
保存でなく削除") was checked for real, and came back negative — see
#5450's PR for the executed evidence. A mode with a real implementation
but zero reachable production scenario, and (once its one committed test
is deleted for the same reason) zero test consumer either, is exactly
the #4866/#5442 "public surface nobody calls" shape this codebase has
repeatedly closed rather than ratified — so it was never added, not
added-then-removed.

Unlimited await on ``call_started`` (no ``sleep``/``timeout``/``range(N)``,
testing-policy time discipline) is how a test proves the real loop
actually reached the LLM boundary — joined with #5454's ``turn_started``
audit-event for "did the REAL driver run" (a stub being called is not,
by itself, proof of that — see #5450's own witness ②).

#5382 extension — the compaction "raise mode"
-----------------------------------------------
``CompactionEngine.compact()``'s request payload is built ENTIRELY inside
``engine.py`` (candidates selected by ``CompactionController._select_
candidates``, itself derived from the real engine's own computed budgets)
— a test cannot predict, and therefore cannot key a fixture against, that
exact payload without duplicating that selection logic (the #5382 dead
end: reachable only via ``LLMReplay``, whose fixture entries ARE keyed by
exact payload hash). ``LLMStub`` has no such key (see above — it reads no
fixture at all), so it is the right layer to add selective behavior to
INSTEAD of teaching ``LLMReplay`` to match on something other than a
content hash.

Architect ruling (#5382, quoting the ONE fact this hinges on): the
compaction call's own system message is a FIXED constant
(``reyn.prompt.compaction.COMPACTION_SYSTEM_PROMPT``, ``engine.py:964`` /
``:1538``), never derived from conversation content. That constant — not a
payload hash — is the one discriminator usable at the litellm boundary to
tell "this is a compaction call" apart from "this is the main router's own
call", since ``purpose="compaction"`` (the #1190 cost-attribution tag) is a
reyn-layer argument that never reaches litellm's own call signature.
Patching ``recorded_acompletion`` instead (one layer up, where ``purpose=``
IS visible) was explicitly rejected — it would skip #1190's own cost
recording, silently.

``raise_for="compaction"`` + ``cause=<#5382 vocabulary member>`` makes ONLY
compaction calls raise a real litellm exception (reusing #5382's own
closed cause vocabulary — ``reyn.dev.testing.replay``'s
``_REPLAY_EXCEPTION_CAUSES`` — rather than declaring a second one: one
place answers "what can a stub/fixture reproduce"). Every other call
(chiefly the main router's) keeps the ordinary, unconditional-empty-
content success response, UNCHANGED. When a compaction call is recognized
but NOT told to raise, it gets a MINIMAL VALID summary response instead of
the ordinary empty one — ``compact()`` requires non-empty JSON with a
``topic_arc`` (#4883's own validation), so the plain "" response this
stub gives every other call would ALWAYS fail compaction's own parsing
(confirmed empirically while building this: ``ValueError: compaction LLM
returned empty response``) — a compaction call recognized as such must
get a response its own caller can actually accept, or "compaction
succeeds" could never be expressed through this stub at all.

Not a second ``LLMReplay`` wildcard (#5103 C1's own rejection does not
apply): this mode lives entirely OUTSIDE the fixture mechanism — no key,
no fixture file, invisible to ``MissingFixture``/#5283 by the same
construction the rest of this module already has.

``raise_for`` generalization — a caller-supplied content predicate
(architect ruling, same #5382 arc): ``raise_for="compaction"`` was
ALREADY a content predicate (``_is_compaction_call``, fixed to one
constant) — not a new axis, a generalization of the one that already
existed. ``raise_for=<callable(messages) -> bool>`` lets a caller supply
its OWN predicate over the request content directly, for a scenario
`"compaction"`'s fixed check cannot express: "raise on THIS specific
compaction content, succeed on a DIFFERENT one" (e.g. a spill-recovery
test where compact() must fail on an oversized turn, then succeed once
that turn's content has been replaced). ``#5103 C1``'s wildcard rejection
still does not apply for the same reason as above — no fixture mechanism
is touched either way.

Acceptance condition (architect): the predicate receives ``messages``
ONLY — never a call count, never internal state. Deciding by "how many
times has this been called" resurrects the exact weak witness architect
corrected in #5386 (a counter that increments at ``compact()``'s own
entry proves nothing about whether the right CONTENT arrived) — the
consumer test's own main-call fake (``_ContentDrivenLoop`` in
``test_5296_pr2_byte_reduction_same_turn_retry.py``) already lives by
the same rule ("never a hardcoded call-count script"); this predicate
form makes the compaction axis match that same content-driven shape.

``control=`` (#5450) and ``raise_for=``/``cause=`` (#5382) are two
INDEPENDENT axes on the same stub, validated and applied independently —
a test that needs BOTH (hang-then-release a main call, or gate a
compaction call, etc.) may pass both; today's actual callers use at most
one at a time, but nothing here couples them.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Literal, Union

#: #5450: the closed vocabulary for `control=` — mirrors #5382's own
#: closed `cause` vocabulary. A value outside this set raises at
#: LLMStub construction time (fail fast, never a silently-ignored typo).
#: Currently one member — see the module docstring for why
#: "gated_swallow_cancel" was designed but never added.
LLMStubControl = Literal["gated"]
_CONTROL_MODES: "frozenset[str]" = frozenset({"gated"})


class UnknownLLMStubControlError(ValueError):
    """Raised when ``control=`` is not one of :data:`_CONTROL_MODES`."""


#: #5382: the closed vocabulary of NAMED call KINDS this stub can
#: selectively recognize and raise for. Currently one member — closed so
#: a future caller cannot silently invent a second discriminator shape
#: (a payload hash, a free-form string) without a design ruling, the
#: same posture #5382's own ``_REPLAY_EXCEPTION_CAUSES`` vocabulary
#: already takes for *what* raises, applied here to *which call* raises.
#: A caller needing a NARROWER predicate than "compaction" (e.g. "this
#: compaction call, not that one") supplies a callable instead — see
#: :data:`RaiseFor` and the module docstring's "raise_for generalization"
#: section.
_NAMED_RAISE_FOR: "frozenset[str]" = frozenset({"compaction"})

#: A caller-supplied predicate: receives the request's ``messages`` list
#: ONLY (never a call count — see the module docstring's acceptance
#: condition) and returns whether THIS call should raise.
RaiseForPredicate = Callable[[list], bool]

#: Either a NAMED call kind (:data:`_NAMED_RAISE_FOR`'s vocabulary) or a
#: caller-supplied content predicate.
RaiseFor = Union[Literal["compaction"], RaiseForPredicate]


class LLMStub:
    """Install/restore a fixture-less ``litellm.acompletion`` stub.

    Usage (mirrors ``LLMReplay``, via ``tests/conftest.py``)::

        stub = LLMStub()
        stub.install()
        try:
            ...
        finally:
            stub.restore()

    #5450: pass ``control="gated"`` (see module docstring) to hang the
    call at the real LLM boundary until the test sets ``stub.release``;
    ``stub.call_started`` fires the instant the call begins.
    ``control=None`` (default) keeps #5103's original immediate-return
    behavior unchanged.

    #5382: ``raise_for``/``cause`` (both required together, or both
    omitted) select ONE call kind to raise for — see the module docstring
    for the full design. ``cause`` is validated lazily, on the first
    matching call (mirrors ``LLMReplay``'s own ``UnknownReplayCause`` —
    diagnose at the point of use, not eagerly at construction, so the
    error message can name the actual call that hit it).
    """

    def __init__(
        self, *,
        control: "LLMStubControl | None" = None,
        raise_for: "RaiseFor | None" = None,
        cause: "str | None" = None,
    ) -> None:
        if control is not None and control not in _CONTROL_MODES:
            raise UnknownLLMStubControlError(
                f"control={control!r} is not one of {sorted(_CONTROL_MODES)!r}",
            )
        self.control = control
        if (raise_for is None) != (cause is None):
            raise ValueError(
                "LLMStub: raise_for and cause must be given together "
                f"(raise_for={raise_for!r}, cause={cause!r})"
            )
        self._raise_for = raise_for
        self._cause = cause
        self._original_acompletion: Any = None
        # #5450: created unconditionally (cheap) so a test can read
        # `.call_started`/`.release` even before `install()` runs — mirrors
        # the original private helper's own return-both-events shape.
        self.call_started: "asyncio.Event" = asyncio.Event()
        self.release: "asyncio.Event" = asyncio.Event()

    def install(self) -> None:
        import litellm

        self._original_acompletion = litellm.acompletion
        litellm.acompletion = self._handle  # type: ignore[attr-defined]

    def restore(self) -> None:
        import litellm

        if self._original_acompletion is not None:
            litellm.acompletion = self._original_acompletion  # type: ignore[attr-defined]
            self._original_acompletion = None

    async def _handle(self, model: str, messages: list[dict], **kwargs: Any) -> Any:
        # kwargs intentionally unused — every call gets a fixed completion
        # regardless of what was asked, EXCEPT the two selective axes
        # documented above (which call KIND for raise_for, hang-then-
        # release for control — never which call CONTENT). Kept as a
        # named param (not **_) because litellm's real callers invoke
        # acompletion with keyword arguments. `messages` stays live (not
        # deleted) — `_is_compaction_call` below reads it.
        del kwargs
        if self.control == "gated":
            # #5450: fire call_started the instant the call begins, then
            # suspend on release — simulating the real await
            # litellm.acompletion would be suspended inside. A
            # CancelledError delivered here propagates normally (reyn's
            # own code never swallows one here — see module docstring on
            # why "gated_swallow_cancel" was never added).
            self.call_started.set()
            await self.release.wait()
        import litellm

        if self._raise_for is not None and _should_raise(self._raise_for, messages):
            # __init__ guarantees raise_for/cause are set together — cause
            # is never None here, but mypy can't see that invariant across
            # the two attributes. A type-level cast, not a runtime check
            # (nothing to strip under -O): __init__'s own ValueError already
            # enforces this at construction time.
            from typing import cast

            from reyn.dev.testing.replay import _REPLAY_EXCEPTION_CAUSES, UnknownReplayCause

            cause = cast(str, self._cause)
            factory = _REPLAY_EXCEPTION_CAUSES.get(cause)
            if factory is None:
                raise UnknownReplayCause(
                    f"LLMStub(raise_for={self._raise_for!r}, cause={cause!r}): "
                    f"not in the closed replay vocabulary "
                    f"{sorted(_REPLAY_EXCEPTION_CAUSES)!r}."
                )
            raise factory(f"stubbed {cause} (LLMStub raise_for={self._raise_for!r})")

        if _is_compaction_call(messages):
            # #4883: compact() requires non-empty JSON with `topic_arc` (and
            # the other 4 required array fields) — the ordinary "" response
            # below would ALWAYS fail this call's own validation.
            content = json.dumps({
                "topic_arc": "stub summary", "decisions": [], "pending": [],
                "session_user_facts": [], "artifacts_referenced": [],
            })
        else:
            # #5103 TESTS-READ: finish_reason/tool_calls are set EXPLICITLY
            # here — this is OUR contract, not litellm.ModelResponse's own
            # default (see module docstring). content="" (not None) so a
            # caller that does `response.choices[0].message.content or ""`
            # -style handling sees an ordinary empty string, not an absent
            # field.
            content = ""
        message = litellm.Message(content=content, role="assistant", tool_calls=None)
        choice = litellm.Choices(finish_reason="stop", index=0, message=message)
        return litellm.ModelResponse(model=model, choices=[choice])


def _is_compaction_call(messages: list[dict]) -> bool:
    """True iff ``messages`` is a real ``CompactionEngine.compact()`` call
    — discriminated by its own fixed system-message constant
    (``engine.py:1538``), never by conversation content (see module
    docstring for why this is the only discriminator usable at the
    litellm boundary)."""
    from reyn.prompt.compaction import COMPACTION_SYSTEM_PROMPT

    return bool(messages) and messages[0].get("content") == COMPACTION_SYSTEM_PROMPT


def _should_raise(raise_for: "RaiseFor", messages: list[dict]) -> bool:
    """Resolve ``raise_for`` (a NAMED call kind or a caller-supplied
    predicate — see the module docstring's "raise_for generalization")
    against ``messages``. The named form is itself just
    ``_is_compaction_call`` under a fixed name — both branches are
    content predicates over ``messages``, never a call count."""
    if isinstance(raise_for, str):
        if raise_for not in _NAMED_RAISE_FOR:
            # #5382: closed named vocabulary — a string outside it is a
            # typo, not a silently-ignored no-op.
            raise ValueError(
                f"LLMStub: raise_for={raise_for!r} is not a known named "
                f"call kind ({sorted(_NAMED_RAISE_FOR)!r}) and is not "
                f"callable — pass a callable(messages) -> bool for a "
                f"custom predicate."
            )
        return _is_compaction_call(messages)
    return raise_for(messages)
