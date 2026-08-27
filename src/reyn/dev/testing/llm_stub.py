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
- Every call gets the SAME minimal ``litellm.ModelResponse``: default
  ``finish_reason="stop"``, ``tool_calls=None`` — the loop always sees "the
  model said nothing and asked for nothing", so it completes the turn
  without entering any tool-call branch. A test built for this stub must
  not assert on the completion's own content (that is what makes it not
  Tier 3 — see ``@pytest.mark.llm_stub``'s registration text below).

Only ``litellm.acompletion`` is patched (not ``aembedding``) — the turns
this stub exists for reach the completion boundary, never the embedding
one; a test that needs both should use ``@replay`` instead.
"""
from __future__ import annotations

from typing import Any


class LLMStub:
    """Install/restore a fixture-less ``litellm.acompletion`` stub.

    Usage (mirrors ``LLMReplay``, via ``tests/conftest.py``)::

        stub = LLMStub()
        stub.install()
        try:
            ...
        finally:
            stub.restore()
    """

    def __init__(self) -> None:
        self._original_acompletion: Any = None

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
        import litellm

        return litellm.ModelResponse(model=model)
