"""Tier 2: OS invariant tests for #5527 —
``tests._support.hooks.assert_hook_trigger_signature``.

Real incident (#5516 arc, root-caused #5527): 3 hand-written
``hook_trigger``-shaped test doubles kept the PRE-#5516 single-event
signature after production moved to the batch shape. Each raised
``TypeError`` on the missing kwarg, silently swallowed by ``HookDispatcher``'s
own per-hook isolation ``try/except`` — the caller never saw a red test,
only an unbounded ``_wait_for`` poll that never terminated (a hang, not a
failure).

lead-coder's own BLOCKING finding on this function's first version (PR
#5534, head ce127578e): the ``hook_trigger`` slot has TWO real occupants
with genuinely different signatures —
``HookDispatcher.dispatch_external_batch`` (``skipped_session_wide``
KEYWORD-ONLY) and ``Session._bridge_hook_trigger`` (POSITIONAL-OR-KEYWORD)
— so the helper takes *real* explicitly, never defaults it. This file
exercises BOTH real targets, not just one, so a future change that
silently re-hardcodes a single default cannot pass this file's own tests
while actually being wrong for the other target.

Policy compliance:
- No unittest.mock / MagicMock / AsyncMock / patch.
- No private-state assertions — drives ``assert_hook_trigger_signature``
  through its own public contract only.
- Each docstring opens with ``Tier 2: ...``.
"""
from __future__ import annotations

import pytest

from reyn.hooks.dispatcher import HookDispatcher
from reyn.runtime.session import Session
from tests._support.hooks import assert_hook_trigger_signature

# The two real occupants of the hook_trigger slot (see module docstring) —
# parametrized so every test below runs against BOTH, proving neither one
# is silently favored by the helper's own implementation.
_REAL_TARGETS = [
    pytest.param(HookDispatcher.dispatch_external_batch, id="dispatch_external_batch"),
    pytest.param(Session._bridge_hook_trigger, id="_bridge_hook_trigger"),
]


@pytest.mark.parametrize("real", _REAL_TARGETS)
def test_a_pre_5516_shaped_double_fails_loudly(real) -> None:
    """Tier 2: #5527 accept ① — a double whose signature has drifted from
    *real* (here: the exact #5516-arc drift — ``skipped_session_wide``
    missing entirely) makes ``assert_hook_trigger_signature`` raise,
    immediately and loudly (an ``AssertionError``, not a hang, not a
    swallowed ``TypeError`` three layers downstream inside
    ``HookDispatcher``'s own per-hook isolation) — for EITHER real target."""

    async def _pre_5516_shaped_trigger(point: str, template_vars: dict) -> None:
        pass

    with pytest.raises(AssertionError, match="drifted"):
        assert_hook_trigger_signature(_pre_5516_shaped_trigger, real=real)


def test_a_kind_only_drift_against_dispatch_external_batch_fails() -> None:
    """Tier 2: #5527 accept ① — a double whose ``skipped_session_wide`` is
    POSITIONAL-OR-KEYWORD (the real shape of the OTHER occupant,
    ``Session._bridge_hook_trigger``) is a genuine drift against
    ``dispatch_external_batch``, whose real ``skipped_session_wide`` is
    KEYWORD-ONLY. Name-and-default-only comparison would miss this;
    ``kind`` must be part of the compared shape."""

    async def _bridge_shaped_trigger(
        point: str, payloads: list, skipped_session_wide: int = 0,
    ) -> None:
        pass

    with pytest.raises(AssertionError, match="drifted"):
        assert_hook_trigger_signature(
            _bridge_shaped_trigger, real=HookDispatcher.dispatch_external_batch,
        )


def test_a_kind_only_drift_against_bridge_hook_trigger_fails() -> None:
    """Tier 2: #5527 accept ① — the MIRROR of the test above: a double
    whose ``skipped_session_wide`` is KEYWORD-ONLY (the real shape of
    ``dispatch_external_batch``) is a genuine drift against
    ``Session._bridge_hook_trigger``, whose real ``skipped_session_wide``
    is POSITIONAL-OR-KEYWORD — the SAME regression PR #5534's first
    version actually introduced into ``_RecordingTrigger``/``_Recorder``
    (a double made STRICTER than the real thing it substitutes for)."""

    async def _dispatch_shaped_trigger(
        point: str, payloads: list, *, skipped_session_wide: int = 0,
    ) -> None:
        pass

    with pytest.raises(AssertionError, match="drifted"):
        assert_hook_trigger_signature(
            _dispatch_shaped_trigger, real=Session._bridge_hook_trigger,
        )


def test_a_double_matching_dispatch_external_batch_does_not_fail() -> None:
    """Tier 2: #5527 accept ② (the deny-side sibling) — a double whose
    signature genuinely matches ``dispatch_external_batch`` does NOT raise.
    Without this, an "always raise" implementation would pass accept ①
    for the wrong reason."""

    async def _correctly_shaped_trigger(
        point: str, payloads: list, *, skipped_session_wide: int = 0,
    ) -> None:
        pass

    assert_hook_trigger_signature(
        _correctly_shaped_trigger, real=HookDispatcher.dispatch_external_batch,
    )  # must not raise


def test_a_double_matching_bridge_hook_trigger_does_not_fail() -> None:
    """Tier 2: #5527 accept ② — the MIRROR deny-side proof for the OTHER
    real target: a double whose signature genuinely matches
    ``Session._bridge_hook_trigger`` (POSITIONAL-OR-KEYWORD) does NOT
    raise."""

    async def _correctly_shaped_trigger(
        point: str, payloads: list, skipped_session_wide: int = 0,
    ) -> None:
        pass

    assert_hook_trigger_signature(
        _correctly_shaped_trigger, real=Session._bridge_hook_trigger,
    )  # must not raise


def test_a_matching_instance_double_does_not_fail() -> None:
    """Tier 2: #5527 accept ② — the SAME deny-side proof, but for an
    instance whose ``__call__`` is correctly shaped (the actual double
    shape both #5527-fixed doubles use, not a bare function) — proving the
    helper's ``self``-stripping (via ``inspect.signature``'s own bound-
    method behavior on ``__call__``) works for this shape too, not just a
    bare async function."""

    class _CorrectlyShapedRecorder:
        async def __call__(
            self, point: str, payloads: list, skipped_session_wide: int = 0,
        ) -> None:
            pass

    assert_hook_trigger_signature(
        _CorrectlyShapedRecorder(), real=Session._bridge_hook_trigger,
    )  # must not raise


def test_annotation_differences_alone_do_not_fail() -> None:
    """Tier 2: #5527 — the helper deliberately does NOT compare type
    annotations (see ``_param_shape``'s own docstring): a double with no
    annotations at all (the common case — hand-written test doubles rarely
    repeat production's own string-quoted type hints) must still pass when
    its name/kind/has-default shape genuinely matches."""

    async def _unannotated_trigger(point, payloads, skipped_session_wide=0):
        pass

    assert_hook_trigger_signature(
        _unannotated_trigger, real=Session._bridge_hook_trigger,
    )  # must not raise
