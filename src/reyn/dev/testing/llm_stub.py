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
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal

#: #5450: the closed vocabulary for `control=` — mirrors #5382's own
#: closed `cause` vocabulary. A value outside this set raises at
#: LLMStub construction time (fail fast, never a silently-ignored typo).
#: Currently one member — see the module docstring for why
#: "gated_swallow_cancel" was designed but never added.
LLMStubControl = Literal["gated"]
_CONTROL_MODES: "frozenset[str]" = frozenset({"gated"})


class UnknownLLMStubControlError(ValueError):
    """Raised when ``control=`` is not one of :data:`_CONTROL_MODES`."""


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
    """

    def __init__(self, *, control: "LLMStubControl | None" = None) -> None:
        self._original_acompletion: Any = None
        if control is not None and control not in _CONTROL_MODES:
            raise UnknownLLMStubControlError(
                f"control={control!r} is not one of {sorted(_CONTROL_MODES)!r}",
            )
        self.control = control
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
        # messages/kwargs intentionally unused — every call gets the same
        # minimal completion regardless of what was asked (see module
        # docstring). Kept as named params (not *args, **_) because litellm's
        # real callers invoke acompletion with keyword arguments.
        del messages, kwargs
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

        # #5103 TESTS-READ: finish_reason/tool_calls are set EXPLICITLY here
        # — this is OUR contract, not litellm.ModelResponse's own default
        # (see module docstring). content="" (not None) so a caller that
        # does `response.choices[0].message.content or ""`-style handling
        # sees an ordinary empty string, not an absent field.
        message = litellm.Message(content="", role="assistant", tool_calls=None)
        choice = litellm.Choices(finish_reason="stop", index=0, message=message)
        return litellm.ModelResponse(model=model, choices=[choice])
