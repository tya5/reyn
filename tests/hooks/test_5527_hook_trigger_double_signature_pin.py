"""Tier 2: OS invariant tests for #5527 —
``tests._support.hooks.assert_hook_trigger_signature``.

Real incident (#5516 arc, root-caused #5527): 3 hand-written
``hook_trigger``-shaped test doubles kept the PRE-#5516 single-event
signature after production moved to the batch shape
(``HookDispatcher.dispatch_external_batch(point, payloads, *,
skipped_session_wide=0)``). Each raised ``TypeError`` on the missing
kwarg, silently swallowed by ``HookDispatcher``'s own per-hook isolation
``try/except`` — the caller never saw a red test, only an unbounded
``_wait_for`` poll that never terminated (a hang, not a failure). Architect's
prescription (settled, #5527's own issue thread): pin a double's signature
against the real target ONCE, inside the helper that constructs the double
— this file proves that pin actually does the job it is for.

Policy compliance:
- No unittest.mock / MagicMock / AsyncMock / patch.
- No private-state assertions — drives ``assert_hook_trigger_signature``
  through its own public contract only.
- Each docstring opens with ``Tier 2: ...``.
"""
from __future__ import annotations

import pytest

from tests._support.hooks import assert_hook_trigger_signature


def test_a_drifted_double_signature_fails_loudly() -> None:
    """Tier 2: #5527 accept ① — a double whose signature has drifted from
    the real ``dispatch_external_batch`` (here: the exact #5516-arc drift —
    ``skipped_session_wide`` missing entirely) makes
    ``assert_hook_trigger_signature`` raise, immediately and loudly (an
    ``AssertionError``, not a hang, not a swallowed ``TypeError`` three
    layers downstream inside ``HookDispatcher``'s own per-hook isolation)."""

    async def _pre_5516_shaped_trigger(point: str, template_vars: dict) -> None:
        pass

    with pytest.raises(AssertionError, match="drifted"):
        assert_hook_trigger_signature(_pre_5516_shaped_trigger)


def test_a_kind_only_drift_also_fails() -> None:
    """Tier 2: #5527 accept ① — the SPECIFIC drift #5527's own investigation
    found in the 2 real doubles this issue fixed: ``skipped_session_wide``
    present, correctly named and defaulted, but declared POSITIONAL-OR-
    KEYWORD instead of the real target's KEYWORD-ONLY kind (no leading
    ``*``). Name-and-default-only comparison would miss this; ``kind`` must
    be part of the compared shape."""

    async def _wrong_kind_trigger(
        point: str, payloads: list, skipped_session_wide: int = 0,
    ) -> None:
        pass

    with pytest.raises(AssertionError, match="drifted"):
        assert_hook_trigger_signature(_wrong_kind_trigger)


def test_a_matching_double_does_not_fail() -> None:
    """Tier 2: #5527 accept ② (the deny-side sibling) — a double whose
    signature genuinely matches the real target does NOT raise. Without
    this, an "always raise" implementation of ``assert_hook_trigger_
    signature`` would pass accept ① for the wrong reason."""

    async def _correctly_shaped_trigger(
        point: str, payloads: list, *, skipped_session_wide: int = 0,
    ) -> None:
        pass

    assert_hook_trigger_signature(_correctly_shaped_trigger)  # must not raise


def test_a_matching_instance_double_does_not_fail() -> None:
    """Tier 2: #5527 accept ② — the SAME deny-side proof, but for an
    instance whose ``__call__`` is correctly shaped (the actual double
    shape both #5527-fixed doubles use, not a bare function) — proving the
    helper's ``self``-stripping (via ``inspect.signature``'s own bound-
    method behavior on ``__call__``) works for this shape too, not just a
    bare async function."""

    class _CorrectlyShapedRecorder:
        async def __call__(
            self, point: str, payloads: list, *, skipped_session_wide: int = 0,
        ) -> None:
            pass

    assert_hook_trigger_signature(_CorrectlyShapedRecorder())  # must not raise


def test_annotation_differences_alone_do_not_fail() -> None:
    """Tier 2: #5527 — the helper deliberately does NOT compare type
    annotations (see ``_param_shape``'s own docstring): a double with no
    annotations at all (the common case — hand-written test doubles rarely
    repeat production's own string-quoted type hints) must still pass when
    its name/kind/has-default shape genuinely matches."""

    async def _unannotated_trigger(point, payloads, *, skipped_session_wide=0):
        pass

    assert_hook_trigger_signature(_unannotated_trigger)  # must not raise
