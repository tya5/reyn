"""Tests for #1800 slice A — hook config schema + loader + registry.

Coverage plan
-------------
Tier 1 (contract): ``reyn.yaml hooks:`` schema acceptance/rejection
  + ``HookDef`` / ``PushBlock`` shape
  + ``HookRegistry.hooks_for`` registration-order preservation.
Load-from-disk round-trip: a tmp ``reyn.yaml`` with a ``hooks:`` block →
  ``HookRegistry`` with expected ``HookDef`` objects using non-default
  values for every optional field so an unwired field would fail the test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.hooks import (
    HookConfigError,
    HookDef,
    HookRegistry,
    PushBlock,
    load_hooks,
)
from reyn.hooks.schema import ALLOWED_HOOK_POINTS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_push(
    *,
    on: str = "turn_end",
    message: str = "test message",
    wake: bool | str = True,
    push_when: str = "true",
    session: str | None = None,
    matcher: str | None = None,
) -> dict:
    """Build a raw push-hook dict (valid by default)."""
    push: dict = {"message": message, "wake": wake, "push_when": push_when}
    if session is not None:
        push["session"] = session
    entry: dict = {"on": on, "template_push": push}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def _raw_shell(*, on: str = "session_end", command: str = "echo done") -> dict:
    """Build a raw shell-hook dict (valid by default)."""
    return {"on": on, "shell_exec": command}


# ===========================================================================
# Tier 1 — Contract: HookDef shape
# ===========================================================================


def test_hookdef_push_shape() -> None:
    """Tier 1: ``HookDef`` with a ``PushBlock`` carries the expected fields."""
    push = PushBlock(
        message="{{ event.name }}",
        wake="{{ ctx.needs_wake }}",
        push_when="{{ ctx.condition }}",
        session="session-abc",
    )
    hd = HookDef(on="turn_end", template_push=push, shell_exec=None, matcher="my-matcher")

    assert hd.on == "turn_end"
    assert hd.template_push is push
    assert hd.template_push.message == "{{ event.name }}"
    assert hd.template_push.wake == "{{ ctx.needs_wake }}"
    assert hd.template_push.push_when == "{{ ctx.condition }}"
    assert hd.template_push.session == "session-abc"
    assert hd.matcher == "my-matcher"
    assert hd.shell_exec is None


def test_hookdef_shell_shape() -> None:
    """Tier 1: ``HookDef`` with a shell command carries the expected fields."""
    hd = HookDef(on="session_end", shell_exec="scripts/cleanup.sh", template_push=None)

    assert hd.on == "session_end"
    assert hd.shell_exec == "scripts/cleanup.sh"
    assert hd.template_push is None


def test_hookdef_is_frozen() -> None:
    """Tier 1: ``HookDef`` and ``PushBlock`` are immutable (frozen dataclasses)."""
    hd = HookDef(on="turn_start", shell_exec="echo hi")
    with pytest.raises(Exception):  # FrozenInstanceError
        hd.on = "turn_end"  # type: ignore[misc]

    pb = PushBlock(message="hi")
    with pytest.raises(Exception):
        pb.message = "changed"  # type: ignore[misc]


# ===========================================================================
# Tier 1 — Contract: valid hook definitions accepted
# ===========================================================================


def test_load_hooks_all_allowed_points_accepted() -> None:
    """Tier 1: every point in ``ALLOWED_HOOK_POINTS`` is accepted by the loader."""
    for point in ALLOWED_HOOK_POINTS:
        raw = [_raw_push(on=point)]
        registry = load_hooks(raw)
        hooks = registry.hooks_for(point)
        (hd,) = hooks  # exactly one hook returned — unpack fails on zero or many
        assert hd.on == point


def test_load_hooks_push_minimal_valid() -> None:
    """Tier 1: a push hook with only required ``message`` is accepted."""
    raw = [{"on": "turn_end", "template_push": {"message": "hello"}}]
    registry = load_hooks(raw)
    hooks = registry.hooks_for("turn_end")
    (hd,) = hooks  # exactly one — unpack enforces count
    assert hd.template_push is not None
    assert hd.template_push.message == "hello"
    # Defaults
    assert hd.template_push.wake is True
    assert hd.template_push.push_when == "true"
    assert hd.template_push.session is None


def test_load_hooks_push_all_fields_accepted() -> None:
    """Tier 1: a push hook with all optional fields is accepted and parsed correctly."""
    raw = [
        {
            "on": "task_end",
            "template_push": {
                "message": "{{ task.name }} finished",
                "wake": "{{ ctx.wake_needed }}",
                "push_when": "{{ ctx.should_push }}",
                "session": "{{ ctx.target_session }}",
            },
            "matcher": "my-task-filter",
        }
    ]
    registry = load_hooks(raw)
    hooks = registry.hooks_for("task_end")
    (hd,) = hooks  # exactly one — unpack enforces count
    assert hd.template_push is not None
    assert hd.template_push.message == "{{ task.name }} finished"
    assert hd.template_push.wake == "{{ ctx.wake_needed }}"
    assert hd.template_push.push_when == "{{ ctx.should_push }}"
    assert hd.template_push.session == "{{ ctx.target_session }}"
    assert hd.matcher == "my-task-filter"


def test_load_hooks_shell_valid() -> None:
    """Tier 1: a shell hook is accepted and stores the command raw."""
    raw = [{"on": "session_end", "shell_exec": "scripts/cleanup.sh --force"}]
    registry = load_hooks(raw)
    hooks = registry.hooks_for("session_end")
    (hd,) = hooks  # exactly one — unpack enforces count
    assert hd.shell_exec == "scripts/cleanup.sh --force"
    assert hd.template_push is None


def test_load_hooks_push_wake_bool_false_accepted() -> None:
    """Tier 1: template_push.wake=False (ride-along mode) is accepted."""
    raw = [{"on": "turn_start", "template_push": {"message": "context note", "wake": False}}]
    registry = load_hooks(raw)
    hd = registry.hooks_for("turn_start")[0]
    assert hd.template_push is not None
    assert hd.template_push.wake is False


def test_load_hooks_none_returns_empty_registry() -> None:
    """Tier 1: ``load_hooks(None)`` (= absent ``hooks:`` key) returns an empty registry."""
    registry = load_hooks(None)
    assert registry.hooks_for("turn_end") == []  # behavioral: no hooks registered


def test_load_hooks_empty_list_returns_empty_registry() -> None:
    """Tier 1: ``load_hooks([])`` returns an empty registry."""
    registry = load_hooks([])
    assert registry.hooks_for("session_start") == []  # behavioral: no hooks registered


# ===========================================================================
# Tier 1 — Contract: invalid definitions rejected
# ===========================================================================


def test_load_hooks_bad_hook_point_rejected() -> None:
    """Tier 1: an unrecognised ``on:`` value raises ``HookConfigError``."""
    with pytest.raises(HookConfigError, match="not a recognised hook-point"):
        load_hooks([{"on": "phase_start", "shell_exec": "echo hi"}])


def test_load_hooks_missing_on_field_rejected() -> None:
    """Tier 1: a hook entry missing ``on`` raises ``HookConfigError``."""
    with pytest.raises(HookConfigError, match="on is required"):
        load_hooks([{"shell_exec": "echo hi"}])


def test_load_hooks_both_push_and_shell_rejected() -> None:
    """Tier 1: specifying more than one of template_push / shell_exec / shell_push raises ``HookConfigError``."""
    with pytest.raises(HookConfigError, match="mutually exclusive"):
        load_hooks(
            [
                {
                    "on": "turn_end",
                    "template_push": {"message": "hi"},
                    "shell_exec": "echo hi",
                }
            ]
        )


def test_load_hooks_neither_push_nor_shell_rejected() -> None:
    """Tier 1: an entry with none of template_push / shell_exec / shell_push raises ``HookConfigError``."""
    with pytest.raises(HookConfigError, match="exactly one of"):
        load_hooks([{"on": "turn_end"}])


def test_load_hooks_push_missing_message_rejected() -> None:
    """Tier 1: a push block without ``message`` raises ``HookConfigError``."""
    with pytest.raises(HookConfigError, match="message is required"):
        load_hooks([{"on": "turn_end", "template_push": {}}])


def test_load_hooks_push_empty_message_rejected() -> None:
    """Tier 1: a push block with empty ``message`` raises ``HookConfigError``."""
    with pytest.raises(HookConfigError, match="must not be empty"):
        load_hooks([{"on": "turn_end", "template_push": {"message": "   "}}])


def test_load_hooks_shell_empty_command_rejected() -> None:
    """Tier 1: a shell hook with empty command raises ``HookConfigError``."""
    with pytest.raises(HookConfigError, match="must not be empty"):
        load_hooks([{"on": "session_end", "shell_exec": ""}])


def test_load_hooks_push_wake_wrong_type_rejected() -> None:
    """Tier 1: ``template_push.wake`` with an invalid type (int) raises ``HookConfigError``."""
    with pytest.raises(HookConfigError, match="template_push.wake must be a bool or template string"):
        load_hooks([{"on": "turn_end", "template_push": {"message": "hi", "wake": 42}}])


def test_load_hooks_entry_not_a_mapping_rejected() -> None:
    """Tier 1: a non-mapping entry in the hooks list raises ``HookConfigError``."""
    with pytest.raises(HookConfigError, match="must be a mapping"):
        load_hooks(["not-a-dict"])


def test_load_hooks_non_list_hooks_value_silently_empty(caplog: pytest.LogCaptureFixture) -> None:
    """Tier 1: a non-list ``hooks:`` value logs a warning and returns an empty registry."""
    import logging
    with caplog.at_level(logging.WARNING, logger="reyn.hooks.loader"):
        registry = load_hooks({"on": "turn_end", "template_push": {"message": "hi"}})
    assert registry.hooks_for("turn_end") == []  # behavioral: no hooks despite non-empty input
    assert "must be a list" in caplog.text


def test_load_hooks_error_message_includes_entry_index() -> None:
    """Tier 1: ``HookConfigError`` for the second entry names index [1]."""
    try:
        load_hooks(
            [
                {"on": "turn_end", "template_push": {"message": "ok"}},
                {"on": "bad_point", "shell_exec": "echo"},
            ]
        )
        raise AssertionError("should have raised")
    except HookConfigError as exc:
        assert "[1]" in str(exc)


# ===========================================================================
# Tier 1 — Contract: HookRegistry registration-order preservation
# ===========================================================================


def test_registry_hooks_for_preserves_registration_order() -> None:
    """Tier 1: ``hooks_for`` returns hooks in registration (list) order."""
    raw = [
        {"on": "turn_end", "template_push": {"message": "first"}},
        {"on": "task_start", "shell_exec": "echo a"},
        {"on": "turn_end", "template_push": {"message": "second"}},
        {"on": "turn_end", "shell_exec": "echo b"},
    ]
    registry = load_hooks(raw)
    hooks = registry.hooks_for("turn_end")
    # Exactly three hooks at turn_end — use unpack-enforcement so extra/missing fails
    first, second, third = hooks
    # Order: first push → second push → shell
    assert first.template_push is not None and first.template_push.message == "first"
    assert second.template_push is not None and second.template_push.message == "second"
    assert third.shell_exec == "echo b"


def test_registry_hooks_for_unknown_point_returns_empty() -> None:
    """Tier 1: ``hooks_for`` with an unknown point returns an empty list (no error)."""
    raw = [{"on": "turn_end", "shell_exec": "echo hi"}]
    registry = load_hooks(raw)
    assert registry.hooks_for("agent_start") == []


def test_registry_hooks_for_no_match_returns_empty() -> None:
    """Tier 1: ``hooks_for`` returns an empty list when no hooks match the point."""
    raw = [{"on": "turn_end", "shell_exec": "echo hi"}]
    registry = load_hooks(raw)
    assert registry.hooks_for("task_start") == []


# ===========================================================================
# Load-from-disk round-trip
# ===========================================================================


def test_load_hooks_round_trip_from_yaml(tmp_path: Path) -> None:
    """Tier 1: a ``hooks:`` block in reyn.yaml round-trips to the expected
    ``HookDef`` registry.  Every optional field is set to a non-default value
    so an unwired field would cause the assertion to fail.
    """
    import yaml

    yaml_content = """
hooks:
  - on: task_end
    template_push:
      message: "task {{ task.name }} done"
      wake: false
      push_when: "{{ ctx.should_notify }}"
      session: "{{ ctx.target_session }}"
    matcher: task-done-filter

  - on: session_start
    shell_exec: "scripts/on-session-start.sh"
    matcher: session-filter
""".lstrip()

    reyn_yaml = tmp_path / "reyn.yaml"
    reyn_yaml.write_text(yaml_content, encoding="utf-8")

    raw_cfg = yaml.safe_load(reyn_yaml.read_text(encoding="utf-8"))
    registry = load_hooks(raw_cfg.get("hooks"))

    # ── Hook 1: push hook at task_end ────────────────────────────────────
    task_end_hooks = registry.hooks_for("task_end")
    (h1,) = task_end_hooks  # exactly one — unpack enforces count
    assert h1.on == "task_end"
    assert h1.template_push is not None
    assert h1.template_push.message == "task {{ task.name }} done"
    # non-default wake=False (default is True)
    assert h1.template_push.wake is False
    # non-default push_when template (default is "true")
    assert h1.template_push.push_when == "{{ ctx.should_notify }}"
    # non-default session template (default is None)
    assert h1.template_push.session == "{{ ctx.target_session }}"
    # non-default matcher (default is None)
    assert h1.matcher == "task-done-filter"

    # ── Hook 2: shell hook at session_start ───────────────────────────────
    session_start_hooks = registry.hooks_for("session_start")
    (h2,) = session_start_hooks  # exactly one — unpack enforces count
    assert h2.on == "session_start"
    assert h2.shell_exec == "scripts/on-session-start.sh"
    assert h2.template_push is None
    assert h2.matcher == "session-filter"

    # ── Hooks at other points are empty (no stray registrations) ─────────
    assert registry.hooks_for("turn_end") == []
    assert registry.hooks_for("task_start") == []
    assert registry.hooks_for("turn_start") == []
