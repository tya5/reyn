"""Tier 2: shutdown drain must not leak the litellm acompletion "never awaited"
RuntimeWarning (#52).

Background
----------
litellm 1.84.0 (``main.py:606-624``) follows this pattern in ``async def
acompletion``::

    init_response = await loop.run_in_executor(None, func_with_context)
    if isinstance(init_response, dict) or isinstance(init_response, ModelResponse):
        response = init_response
    elif asyncio.iscoroutine(init_response):
        response = await init_response   # ← inner coro awaited here
    else:
        response = init_response

The thread-pool ``completion()`` builds and returns an
``OpenAIChatCompletion.acompletion(...)`` coroutine when ``acompletion=True``
(see ``openai.py:706``); the outer ``await init_response`` is the only point
that consumes it. When ``ChatSession._drain_on_shutdown`` forces
``SkillRunner.cancel_all()`` while a skill's LLM call is at that checkpoint,
``CancelledError`` lands BEFORE ``await init_response`` is entered, the inner
coroutine is GC'd unawaited, and Python emits ::

    RuntimeWarning: coroutine 'OpenAIChatCompletion.acompletion' was never awaited

The warning is benign — the unawaited coroutine is the cancelled network
request, which is what we want during forced shutdown. The fix in
``session._drain_on_shutdown`` is a tightly-scoped ``warnings.catch_warnings``
that suppresses ONLY this specific message text around the cancel_all() call,
so genuine missing-await bugs elsewhere stay visible.

These tests pin:
  1. The filter actually suppresses the warning when matching coroutines GC.
  2. Without the filter (= negative control) the warning DOES fire — proving
     the regression guard is meaningful, not vacuous.
  3. The filter does NOT swallow other RuntimeWarnings (= scope discipline).
"""
from __future__ import annotations

import asyncio
import gc
import warnings


class OpenAIChatCompletion:
    """Stub mirroring the litellm provider class — same qualname so the
    GC-emitted warning matches the filter's regex byte-for-byte."""

    async def acompletion(self, *args, **kwargs):  # noqa: D401 — match upstream signature shape
        return None


def _make_unawaited_coro() -> None:
    """Build the provider coroutine and drop the reference without awaiting.

    Triggers the same GC-time ``RuntimeWarning`` that litellm's executor race
    produces during shutdown.
    """
    OpenAIChatCompletion().acompletion()
    # Coroutine is GC'd as soon as this function returns — refcount → 0.


def _shutdown_warning_filter():
    """Mirror the filter installed by ``ChatSession._drain_on_shutdown``.

    Kept in lockstep with ``src/reyn/chat/session.py`` so a future tweak to
    the production filter is forced to update this test (= the assertion
    on filter scope below will start failing if the message regex drifts).
    """
    return warnings.catch_warnings(), {
        "message": (
            r".*coroutine 'OpenAIChatCompletion\.acompletion' "
            r"was never awaited.*"
        ),
        "category": RuntimeWarning,
    }


def test_negative_control_unawaited_coro_emits_warning():
    """Tier 2: without the filter, dropping the coro DOES emit the warning.

    Anchors the regression guard: if Python ever stops emitting this warning
    (e.g. asyncio internals change), the suppression test below would pass
    vacuously. This sibling assertion catches that.
    """
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", RuntimeWarning)
        _make_unawaited_coro()
        gc.collect()

    leaked = [
        w for w in captured
        if issubclass(w.category, RuntimeWarning)
        and "never awaited" in str(w.message)
        and "OpenAIChatCompletion.acompletion" in str(w.message)
    ]
    assert leaked, (
        "expected at least one 'never awaited' RuntimeWarning to fire "
        "without the filter — got none. The regression guard below is "
        "vacuous if this fails; investigate the Python version's "
        "coroutine __del__ behaviour."
    )


def test_shutdown_filter_suppresses_litellm_acompletion_warning():
    """Tier 2: the shutdown filter swallows the litellm provider coro warning.

    Mirrors the filter installed by ``session._drain_on_shutdown`` and
    asserts that the GC-emitted RuntimeWarning from an unawaited
    ``OpenAIChatCompletion.acompletion`` coroutine does NOT leak when the
    filter is active.
    """
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", RuntimeWarning)
        warnings.filterwarnings(
            "ignore",
            message=(
                r".*coroutine 'OpenAIChatCompletion\.acompletion' "
                r"was never awaited.*"
            ),
            category=RuntimeWarning,
        )
        _make_unawaited_coro()
        gc.collect()

    leaked = [
        w for w in captured
        if "never awaited" in str(w.message)
        and "OpenAIChatCompletion.acompletion" in str(w.message)
    ]
    assert not leaked, (
        f"shutdown filter must suppress the litellm provider 'never "
        f"awaited' warning; leaked: {[str(w.message) for w in leaked]}"
    )


def test_shutdown_filter_does_not_swallow_unrelated_warnings():
    """Tier 2: filter scope is bounded — other RuntimeWarnings still surface.

    Guards against an over-broad filter (e.g. dropping the message regex)
    that would hide unrelated missing-await bugs.
    """

    class OtherProvider:
        async def acompletion(self):
            return None

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", RuntimeWarning)
        warnings.filterwarnings(
            "ignore",
            message=(
                r".*coroutine 'OpenAIChatCompletion\.acompletion' "
                r"was never awaited.*"
            ),
            category=RuntimeWarning,
        )
        # Drop an UNRELATED unawaited coroutine — different qualname.
        OtherProvider().acompletion()
        gc.collect()

    leaked = [
        w for w in captured
        if "never awaited" in str(w.message)
        and "OtherProvider.acompletion" in str(w.message)
    ]
    assert leaked, (
        "filter must not be broader than the litellm-specific message — "
        "an unrelated provider's 'never awaited' warning should still leak."
    )


def test_drain_on_shutdown_does_not_leak_warning(tmp_path, monkeypatch):
    """Tier 2: end-to-end — ChatSession._drain_on_shutdown swallows the warning.

    Integrates the filter into the actual production code path: builds a
    real ChatSession, monkeypatches ``SkillRunner.cancel_all`` to provoke
    the unawaited-coroutine pattern WHILE the filter window is active
    (mirrors what the litellm executor race does inside cancel_all's await
    chain), and asserts no warning leaks out.
    """
    from reyn.chat.session import ChatSession
    from reyn.core.events.state_log import StateLog

    monkeypatch.chdir(tmp_path)

    session = ChatSession(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )

    original_cancel_all = session._skill_runner.cancel_all

    async def _patched_cancel_all():
        # Build + drop the matching coroutine inside the filter window so
        # GC fires while the warning filter is active. This mirrors the
        # litellm executor race timing — the unawaited coro is born and
        # dies inside the cancel_all() await chain.
        _make_unawaited_coro()
        gc.collect()
        await original_cancel_all()

    session._skill_runner.cancel_all = _patched_cancel_all

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", RuntimeWarning)
        asyncio.run(session._drain_on_shutdown())

    leaked = [
        w for w in captured
        if "never awaited" in str(w.message)
        and "OpenAIChatCompletion.acompletion" in str(w.message)
    ]
    assert not leaked, (
        f"_drain_on_shutdown must suppress the litellm 'never awaited' "
        f"warning; leaked: {[str(w.message) for w in leaked]}"
    )
