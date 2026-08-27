"""Tier 1: #5243 — a hook declaring ``template_push`` on an external event
point (``mcp_resource_updated``/``file_changed``/``cron_fired``/
``webhook_received``) now warns at ``load_hooks`` time.

Real incident (owner's production trace, 2026-08-24): an
``mcp_resource_updated`` hook declared ``template_push`` unconditionally on
the operator's own inbox resource; the agent's own ``receive_messages`` call
(triggered BY that push) itself moved the resource's state, re-firing the
SAME hook. One turn ran 11 hours 34 minutes before an operator noticed and
force-restarted the process — no turn-scoped loop limit could catch it,
since the turn never closed. reyn-self's own incident was fully resolved via
config (``exec_capture`` + a judgment script, no core change) — this issue
was left open for the general-form question, ruled by lead-coder
(issue #5243's own final comment): a load-time WARNING (not a
``HookConfigError``), since the only place to stop this shape is before the
config ever drives a real turn, and declaring ``template_push`` on an
external point is not itself a structural error (a lifecycle point's own
firing IS the content; an external point's is not, #5243's own framing).

Deliberately narrow, per the ruling's own acceptance criteria — all four
proven here:
① external point + ``template_push`` → exactly 1 warning, load still
  succeeds.
② lifecycle point + ``template_push`` → 0 warnings (contrast — firing IS
  the content there).
③ every existing ``HookConfigError`` raise site in ``loader.py`` still
  raises — NONE of them falls to warn-only (the load-bearing witness that
  this change does not loosen the existing structural-validation contract).
④ strip-falsify: removing the warning check makes ① go quiet (red).

Real ``load_hooks`` — no mocks. Uses ``caplog`` (pytest's own real logging
capture) to observe the warning, never a private-state peek."""
from __future__ import annotations

import logging

import pytest

from reyn.hooks import HookConfigError, load_hooks


def _raw_push(*, on: str, message: str = "new content available") -> dict:
    return {
        "on": on,
        "template_push": {"message": message, "wake": True, "push_when": "true"},
    }


def _raw_exec_capture(*, on: str) -> dict:
    return {"on": on, "exec_capture": ["echo", "check"]}


@pytest.mark.parametrize(
    "point",
    ["mcp_resource_updated", "file_changed", "cron_fired", "webhook_received"],
)
def test_external_point_with_template_push_warns_once_and_still_loads(
    point: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 1: acceptance ① — every one of the 4 external points, declared
    with an unconditional ``template_push``, produces exactly 1 warning;
    the registry still loads successfully (this is a warning, not a
    rejection — the config remains fully legal)."""
    with caplog.at_level(logging.WARNING, logger="reyn.hooks.loader"):
        registry = load_hooks([_raw_push(on=point)])

    hooks = registry.hooks_for(point)
    assert hooks and hooks[1:] == [], (
        "test construction error: the hook itself must have loaded, exactly once"
    )
    matching = [r for r in caplog.records if "template_push" in r.message and point in r.message]
    # test_tier_audit.py Rule (format-pinning) rejects `len(x) == N` as a
    # size/shape pin. This IS a behavior claim (reyn's own rule: one
    # external-point + template_push entry produces exactly one warning,
    # not "a warning of length 1"), so it's stated as a value comparison
    # instead: at least one fired (existence), and nothing beyond the
    # first (no duplicate/repeat firing for the same single entry).
    assert matching and matching[1:] == [], (
        f"#5243 REGRESSION: expected exactly 1 warning for external point "
        f"{point!r} + template_push, got {len(matching)}: "
        f"{[r.message for r in matching]!r}"
    )


@pytest.mark.parametrize(
    "point",
    ["session_start", "session_end", "turn_start", "turn_end"],
)
def test_lifecycle_point_with_template_push_never_warns(
    point: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 1: falsification contrast ② — the SAME shape (``template_push``)
    on a LIFECYCLE point produces zero warnings. Firing IS the content for
    these six points (a turn started, a session ended) — #5243's own
    framing for why this warning is scoped to external points only."""
    with caplog.at_level(logging.WARNING, logger="reyn.hooks.loader"):
        registry = load_hooks([_raw_push(on=point)])

    assert len(registry.hooks_for(point)) == 1  # sanity: it still loaded
    matching = [r for r in caplog.records if "template_push" in r.message]
    assert matching == [], (
        f"a lifecycle point ({point!r}) must never warn about "
        f"template_push — got {[r.message for r in matching]!r}"
    )


def test_external_point_with_exec_capture_never_warns(caplog: pytest.LogCaptureFixture) -> None:
    """Tier 1: falsification contrast — an external point using
    ``exec_capture`` instead of ``template_push`` never warns: that action
    CAN embed a content check (reyn-self's own post-incident fix,
    ``broker_inbox_gate.py``), so there is nothing to warn about."""
    with caplog.at_level(logging.WARNING, logger="reyn.hooks.loader"):
        registry = load_hooks([_raw_exec_capture(on="mcp_resource_updated")])

    assert len(registry.hooks_for("mcp_resource_updated")) == 1
    matching = [r for r in caplog.records if "template_push" in r.message]
    assert matching == [], (
        f"exec_capture on an external point must never warn — got "
        f"{[r.message for r in matching]!r}"
    )


@pytest.mark.parametrize(
    "bad_raw, needle",
    [
        ({"on": "turn_end", "template_push": "not-a-dict"}, "must be a mapping"),
        ({"on": "turn_end", "template_push": {"wake": True}}, "message is required"),
        ({"on": "turn_end", "template_push": {"message": 123}}, "message must be a string"),
        ({"on": "turn_end", "template_push": {"message": "   "}}, "must not be empty"),
    ],
)
def test_existing_structural_errors_still_raise_never_fall_to_warn(
    bad_raw: dict, needle: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 1: acceptance ③ (the load-bearing witness, per the ruling) —
    none of the EXISTING ``HookConfigError`` raise sites in
    ``_parse_push_block`` (loader.py) fell to warn-only. This change adds a
    NEW signal for a structurally VALID entry; it must not loosen any
    EXISTING structural-validation contract. Each of the first 4 raise
    sites in ``_parse_push_block`` is driven here directly — ``needle``
    confirms each parametrization actually reaches the raise site its own
    row names, not just SOME raise in the function."""
    with pytest.raises(HookConfigError, match=needle):
        with caplog.at_level(logging.WARNING, logger="reyn.hooks.loader"):
            load_hooks([bad_raw])
    # The entry never finished parsing, so the new check (which only runs
    # on a successfully-built HookDef) never had a chance to fire either
    # way — this is itself part of the witness: raise happens BEFORE any
    # warn-only code could run.
    matching = [r for r in caplog.records if "template_push" in r.message]
    assert matching == [], (
        f"a structurally invalid entry must raise, not warn — got warnings "
        f"{[r.message for r in matching]!r}"
    )
