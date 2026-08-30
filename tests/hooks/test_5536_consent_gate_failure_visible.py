"""Tier 2: #5536 group B — ``HookDispatcher._consent_bus_now``'s own gate
failure is now fail-visible.

architect ruling (#5536, "挙動が変わり、かつ silent" — the real defect this
issue names): ``consent_gate()`` is a caller-supplied, live-evaluated
callable (never frozen at construction — see the dispatcher's own
``__init__`` docstring). Before this fix, a raise there returned ``None``
with NO log. ``None`` routes the shell-hook consent decision to its own
stdin/fail-closed branch (never fail-OPEN — that branch itself was already
safe; architect's own review confirmed this directly, not assumed). But in
an unattended/non-TTY session "ask the operator" silently becomes "refuse
this hook", and nothing recorded WHY.

Accept-side pair (per architect's own instruction — deny side, not "the
log fires"):
- a raising gate reaches a WARNING log naming the fallback direction
  ("fail-closed", never "fail-open") and the exception.
- the sibling positive control: a gate that does NOT raise emits no such
  warning — proving the assertion above isn't vacuously true (a warning
  that always fires regardless would pass the first test for the wrong
  reason).

Note (#5536, architect's own correction): dispatcher.py:537 (the OTHER
group-B site the issue originally named, pipeline_launch's render-failure
catch) already logs a WARNING as of cc8efa267 (2026-08-29 19:55, landed
before architect's own 05:03 review comment the next day) — verified via
``git blame`` before starting this file, not assumed from the issue body.
Only dispatcher.py:184 (this file's own subject) was still silent.

No cadence (#5515's own first-drop/every-Nth discipline does NOT apply
here, architect's own explicit instruction) — this is a FAILURE (an
unexpected raise), not a DROP (an expected, bounded discard); every
occurrence logs.

Real HookDispatcher with a recording run_shell seam, mirroring
test_hook_shell_push_2069.py's own established pattern — no mocks.
"""
from __future__ import annotations

import logging

import pytest

from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef


class _Recorder:
    """A recording async callable (records (args, kwargs); returns None) —
    same shape test_hook_shell_push_2069.py's own ``_Recorder`` uses."""

    def __init__(self) -> None:
        self.calls: "list[tuple[tuple, dict]]" = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _dispatcher(hook: HookDef, *, consent_gate) -> "tuple[HookDispatcher, _Recorder]":
    run_shell = _Recorder()
    disp = HookDispatcher(
        HookRegistry([hook]),
        put_inbox=_Recorder(),
        stage_next_turn_context=_Recorder(),
        run_shell=run_shell,
        consent_bus="a-real-live-bus-object",  # any non-None sentinel
        consent_gate=consent_gate,
    )
    return disp, run_shell


@pytest.mark.asyncio
async def test_consent_gate_raise_logs_the_fail_closed_fallback(caplog):
    """Tier 2: ① — a raising consent_gate reaches a WARNING naming the
    fallback direction, through the real dispatch() → exec path, not by
    calling the private _consent_bus_now() directly.

    Strip-falsify: remove the ``_log.warning(...)`` call in
    ``_consent_bus_now``'s own except branch and this test goes RED — no
    WARNING record at all (performed during review)."""
    def _raising_gate() -> bool:
        raise RuntimeError("listener registry corrupted")

    hook = HookDef(on="turn_end", exec=("true",))
    disp, run_shell = _dispatcher(hook, consent_gate=_raising_gate)

    with caplog.at_level(logging.WARNING):
        await disp.dispatch("turn_end", {})

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    (only,) = warnings  # exactly one warning — unpack-must-flip
    assert "fail-closed" in only.message
    assert "fail-open" not in only.message.lower() or "never fail-open" in only.message
    assert "RuntimeError" in only.message

    # the dispatch itself was never blocked by the gate's own raise — the
    # exec hook still ran, consent_bus falling back to None (the SAME
    # value a caller with no consent machinery at all would see).
    (call,) = run_shell.calls
    _args, kwargs = call
    assert kwargs["consent_bus"] is None


@pytest.mark.asyncio
async def test_a_non_raising_gate_logs_nothing(caplog):
    """Tier 2: ② — deny-side positive control. A consent_gate that
    returns normally (whichever way) must not emit the group-B warning —
    without this, ①'s assertion could pass under a broken implementation
    that warns on EVERY dispatch regardless of whether the gate actually
    raised."""
    hook = HookDef(on="turn_end", exec=("true",))
    disp, run_shell = _dispatcher(hook, consent_gate=lambda: True)

    with caplog.at_level(logging.WARNING):
        await disp.dispatch("turn_end", {})

    assert caplog.records == []
    (call,) = run_shell.calls
    _args, kwargs = call
    assert kwargs["consent_bus"] == "a-real-live-bus-object"
