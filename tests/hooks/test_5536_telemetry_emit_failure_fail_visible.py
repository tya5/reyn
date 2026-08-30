"""Tier 2: #5536 group A — the 4 best-effort telemetry ``emit_event`` catch
sites are now fail-visible.

architect ruling (#5536): a per-hook exception-swallow layer whose silence
loses OBSERVATION-only information (group A, telemetry) may STAY silent
toward the caller — the hook itself must never break because its own
telemetry emit failed — but the failure needs a channel other than
telemetry to report it (auditing a telemetry failure through an
audit-event would be its own quieter self-concealment). Processed: bump
each site's ``_log.debug`` to ``_log.warning``, with the rationale written
ONCE (``shell_runner.py``'s own ``_report_unapplied_agent_policy`` except
branch) and the other 3 sites pointing back to it.

The 4 sites (architect's own census, #5536 issue body):
1. ``shell_runner.py`` — ``_report_unapplied_agent_policy``'s own
   ``sandbox_policy_not_applied`` emit.
2. ``shell_runner.py`` — ``run_shell_hook``'s own ``hook_shell_executed``
   emit.
3. ``dispatcher.py`` — ``_push_resolved``'s own ``hook_push_rejected_
   oversized`` emit.
4. ``dispatcher.py`` — ``_push_resolved``'s own ``hook_push_fired`` emit.

No cadence (same posture as #5536 group B — this is a FAILURE, not a
DROP; every occurrence logs). Accept-side: "log を消したら赤になる test"
(architect's own instruction) — each test's own strip-falsify is
documented inline.

Real ``HookDispatcher``/``load_hooks``/``run_shell_hook`` — no mocks,
reusing test_hook_sandbox_policy_legibility_3005.py's own established
``_dispatch`` recipe for site 1, and test_5536_consent_gate_failure_
visible.py's own recording-seam pattern for sites 3/4.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from reyn.config.infra import SandboxConfig
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.loader import load_hooks
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef, PushBlock
from reyn.security.sandbox import NoopBackend, SandboxPolicy

_PY = sys.executable


def _raising_emit(*_args, **_kwargs):
    raise RuntimeError("telemetry sink is down")


class _Recorder:
    """A recording async callable (no MagicMock/patch) — same shape
    test_5536_consent_gate_failure_visible.py's own ``_Recorder`` uses."""

    def __init__(self) -> None:
        self.calls: "list[tuple[tuple, dict]]" = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


# ---------------------------------------------------------------------------
# Site 1: shell_runner.py — sandbox_policy_not_applied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_policy_not_applied_emit_failure_is_fail_visible(caplog):
    """Tier 2: a raising emit_event on the sandbox_policy_not_applied path
    reaches a WARNING, through the real dispatch() -> exec path (not the
    private function directly) — mirrors test_hook_sandbox_policy_
    legibility_3005.py's own ``_dispatch`` recipe for producing this exact
    event, with a raising sink instead of a recording one.

    Strip-falsify: revert ``_report_unapplied_agent_policy``'s own
    ``_log.warning`` back to ``_log.debug`` (or remove the call) and this
    test goes RED — caplog at WARNING level records nothing (performed
    during review)."""
    hooks = load_hooks([{"on": "turn_end", "exec": ["echo", "hi"]}])  # network omitted
    dispatcher = HookDispatcher(
        hooks,
        put_inbox=lambda *a, **k: None,
        stage_next_turn_context=lambda *a, **k: None,
        sandbox_backend=NoopBackend(),
        sandbox_config=SandboxConfig(backend="noop", policy={"network": True}),
        emit_event=_raising_emit,
    )

    with caplog.at_level(logging.WARNING):
        await dispatcher.dispatch("turn_end", {})

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("emit_event failed" in r.message for r in warnings), (
        f"expected an emit_event-failure WARNING, got: {[r.message for r in warnings]!r}"
    )


# ---------------------------------------------------------------------------
# Site 2: shell_runner.py — hook_shell_executed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_shell_executed_emit_failure_is_fail_visible(
    tmp_path: Path, caplog, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: a raising emit_event on run_shell_hook's own hook_shell_
    executed path reaches a WARNING — real subprocess (a trivial ``true``
    equivalent), not a stub.

    Strip-falsify: revert this site's own ``_log.warning`` back to
    ``_log.debug`` and this test goes RED (performed during review)."""
    from reyn.hooks.shell_runner import run_shell_hook

    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    # check_subprocess_reyn_pin.py: this spawn genuinely never touches
    # `reyn` — `-c "pass"` is the whole program, no import of any kind —
    # so no `out_of_process_reyn`/`reyn_console_scripts` PYTHONPATH pin
    # applies here (that fixture is for a subprocess that DOES import
    # reyn, #5028's own scope). Baselined via --write-baseline.
    with caplog.at_level(logging.WARNING):
        await run_shell_hook(
            [_PY, "-c", "pass"],
            event_context={"events": [{"event": "turn_end"}], "skipped_session_wide": 0},
            timeout_seconds=10,
            sandbox_backend=NoopBackend(),
            sandbox_policy=SandboxPolicy(network=False, deny_subprocess=True, timeout_seconds=10),
            allowlist_path=tmp_path / "allowlist.json",
            emit_event=_raising_emit,
        )

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("emit_event failed" in r.message for r in warnings), (
        f"expected an emit_event-failure WARNING, got: {[r.message for r in warnings]!r}"
    )


# ---------------------------------------------------------------------------
# Sites 3/4: dispatcher.py — hook_push_rejected_oversized / hook_push_fired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_rejected_oversized_emit_failure_is_fail_visible(caplog):
    """Tier 2: a raising emit_event on the hook_push_rejected_oversized
    path (an oversized ``spillability: never`` push, #5514 §5/§8) reaches
    a WARNING.

    Strip-falsify: revert this site's own ``_log.warning`` back to
    ``_log.debug`` and this test goes RED (performed during review)."""
    from reyn.runtime.chat_message import Spillability

    hook = HookDef(
        name="oversized", on="turn_end",
        template_push=PushBlock(message="X" * 500, wake=True),
        spillability=Spillability.NEVER, spillability_max_chars=100,
    )
    disp = HookDispatcher(
        HookRegistry([hook]),
        put_inbox=_Recorder(),
        stage_next_turn_context=_Recorder(),
        emit_event=_raising_emit,
    )

    with caplog.at_level(logging.WARNING):
        await disp.dispatch("turn_end", {})

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "push_rejected_oversized emit_event failed" in r.message for r in warnings
    ), f"expected the specific WARNING, got: {[r.message for r in warnings]!r}"


@pytest.mark.asyncio
async def test_push_fired_emit_failure_is_fail_visible(caplog):
    """Tier 2: a raising emit_event on the hook_push_fired path (a normal,
    within-cap push) reaches a WARNING.

    Strip-falsify: revert this site's own ``_log.warning`` back to
    ``_log.debug`` and this test goes RED (performed during review)."""
    hook = HookDef(name="ok", on="turn_end", template_push=PushBlock(message="hi", wake=True))
    disp = HookDispatcher(
        HookRegistry([hook]),
        put_inbox=_Recorder(),
        stage_next_turn_context=_Recorder(),
        emit_event=_raising_emit,
    )

    with caplog.at_level(logging.WARNING):
        await disp.dispatch("turn_end", {})

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("push_fired emit_event failed" in r.message for r in warnings), (
        f"expected the specific WARNING, got: {[r.message for r in warnings]!r}"
    )


@pytest.mark.asyncio
async def test_a_working_emit_event_never_logs_an_emit_failure_warning(caplog):
    """Tier 2: deny-side positive control (lead-coder BLOCKING, #5571,
    2026-08-30) — the 4 tests above are all accept-side (a raising
    emit_event reaches a WARNING); none of them rule out an
    implementation that warns "emit_event failed" on EVERY dispatch
    regardless of whether emit_event actually raised. This test drives a
    real dispatch with a real, NON-raising emit_event and asserts zero
    "emit_event failed" WARNINGs — the same deny-side-vs-strip-falsify
    distinction this session's own #5566 review already named ("without
    this, ①'s assertion could pass under a broken implementation that
    warns on EVERY dispatch regardless") applied to group A's own 4
    sites: strip-falsify proves "not doing nothing"; this proves "not
    always firing" — two different failure modes, caught by two
    different tests."""
    hook = HookDef(name="ok", on="turn_end", template_push=PushBlock(message="hi", wake=True))
    events: "list[tuple[str, dict]]" = []
    disp = HookDispatcher(
        HookRegistry([hook]),
        put_inbox=_Recorder(),
        stage_next_turn_context=_Recorder(),
        emit_event=lambda et, **d: events.append((et, d)),  # real, never raises
    )

    with caplog.at_level(logging.WARNING):
        await disp.dispatch("turn_end", {})

    assert [et for et, _ in events] == ["hook_push_fired"], (
        "test setup sanity: the dispatch must actually reach the push_fired "
        "emit for this deny-side check to mean anything"
    )
    failure_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "emit_event failed" in r.message
    ]
    assert failure_warnings == []
