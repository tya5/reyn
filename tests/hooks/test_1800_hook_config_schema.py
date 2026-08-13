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
    matcher: "dict[str, str] | None" = None,
) -> dict:
    """Build a raw push-hook dict (valid by default)."""
    push: dict = {"message": message, "wake": wake, "push_when": push_when}
    if session is not None:
        push["session"] = session
    entry: dict = {"on": on, "template_push": push}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def _raw_shell(*, on: str = "session_end", argv: "list[str] | None" = None) -> dict:
    """Build a raw exec-hook dict (valid by default; argv-list-only, #3226 P4)."""
    return {"on": on, "exec": list(argv) if argv is not None else ["echo", "done"]}


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
    # matcher uses a schema-VALID field for turn_end (agent_name) — Phase-3
    # load-time validation flags a schema-external field, so fixtures use a
    # field the point's builtin schema actually carries (this HookDef is built
    # directly, not via load_hooks, but keep it schema-valid for consistency).
    hd = HookDef(
        on="turn_end", template_push=push, exec=None, matcher={"agent_name": "my-agent"}
    )

    assert hd.on == "turn_end"
    assert hd.template_push is push
    assert hd.template_push.message == "{{ event.name }}"
    assert hd.template_push.wake == "{{ ctx.needs_wake }}"
    assert hd.template_push.push_when == "{{ ctx.condition }}"
    assert hd.template_push.session == "session-abc"
    assert hd.matcher == {"agent_name": "my-agent"}
    assert hd.exec is None


def test_hookdef_shell_shape() -> None:
    """Tier 1: ``HookDef`` with an exec argv carries the expected fields (#3226 P4:
    argv-list-only, renamed from ``shell_exec``)."""
    hd = HookDef(on="session_end", exec=("scripts/cleanup.sh",), template_push=None)

    assert hd.on == "session_end"
    assert hd.exec == ("scripts/cleanup.sh",)
    assert hd.template_push is None


def test_hookdef_is_frozen() -> None:
    """Tier 1: ``HookDef`` and ``PushBlock`` are immutable (frozen dataclasses)."""
    hd = HookDef(on="turn_start", exec=("echo", "hi"))
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
            "on": "turn_end",
            "template_push": {
                "message": "{{ chain_id }} finished",
                "wake": "{{ ctx.wake_needed }}",
                "push_when": "{{ ctx.should_push }}",
                "session": "{{ ctx.target_session }}",
                "include": ["chain_id", "agent_name"],
            },
            # schema-valid field for turn_end (chain_id) — Phase-3 load-time
            # validation would reject a schema-external field.
            "matcher": {"chain_id": "my-chain-filter"},
        }
    ]
    registry = load_hooks(raw)
    hooks = registry.hooks_for("turn_end")
    (hd,) = hooks  # exactly one — unpack enforces count
    assert hd.template_push is not None
    assert hd.template_push.message == "{{ chain_id }} finished"
    assert hd.template_push.wake == "{{ ctx.wake_needed }}"
    assert hd.template_push.push_when == "{{ ctx.should_push }}"
    assert hd.template_push.session == "{{ ctx.target_session }}"
    assert hd.template_push.include == ("chain_id", "agent_name")
    assert hd.matcher == {"chain_id": "my-chain-filter"}


def test_load_hooks_push_include_defaults_to_empty() -> None:
    """Tier 1: accept-side non-vacuity for the previous test — a push hook
    with NO ``include:`` key parses to the empty-tuple default (proposal
    0067 P2), not an error and not None."""
    raw = [{"on": "turn_end", "template_push": {"message": "m"}}]
    registry = load_hooks(raw)
    (hd,) = registry.hooks_for("turn_end")
    assert hd.template_push is not None
    assert hd.template_push.include == ()


def test_load_hooks_push_include_non_list_rejected() -> None:
    """Tier 1: ``include`` must be a list — a bare string (an easy operator
    typo, e.g. writing ``include: chain_id`` instead of ``include:
    [chain_id]``) is rejected rather than silently iterated character-by-
    character."""
    raw = [{"on": "turn_end", "template_push": {"message": "m", "include": "chain_id"}}]
    with pytest.raises(HookConfigError, match="include"):
        load_hooks(raw)


def test_load_hooks_push_include_non_string_element_rejected() -> None:
    """Tier 1: falsification pair — a list whose elements are the wrong
    type (not field-name strings) is rejected, not silently accepted."""
    raw = [{"on": "turn_end", "template_push": {"message": "m", "include": [123]}}]
    with pytest.raises(HookConfigError, match="include"):
        load_hooks(raw)


def test_load_hooks_shell_valid() -> None:
    """Tier 1: an exec hook is accepted and stores the argv as a tuple (#3226 P4:
    argv-list-only — a clean break from the pre-Phase-4 shell-command string)."""
    raw = [{"on": "session_end", "exec": ["scripts/cleanup.sh", "--force"]}]
    registry = load_hooks(raw)
    hooks = registry.hooks_for("session_end")
    (hd,) = hooks  # exactly one — unpack enforces count
    assert hd.exec == ("scripts/cleanup.sh", "--force")
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
        load_hooks([{"on": "phase_start", "exec": ["echo", "hi"]}])


def test_load_hooks_missing_on_field_rejected() -> None:
    """Tier 1: a hook entry missing ``on`` raises ``HookConfigError``."""
    with pytest.raises(HookConfigError, match="on is required"):
        load_hooks([{"exec": ["echo", "hi"]}])


def test_load_hooks_both_push_and_shell_rejected() -> None:
    """Tier 1: specifying more than one of template_push / exec / exec_capture raises ``HookConfigError``."""
    with pytest.raises(HookConfigError, match="mutually exclusive"):
        load_hooks(
            [
                {
                    "on": "turn_end",
                    "template_push": {"message": "hi"},
                    "exec": ["echo", "hi"],
                }
            ]
        )


def test_load_hooks_neither_push_nor_shell_rejected() -> None:
    """Tier 1: an entry with none of template_push / exec / exec_capture raises ``HookConfigError``."""
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
    """Tier 1: an exec hook with an empty argv list raises ``HookConfigError``
    (#3226 P4: argv-list-only — an empty list, not an empty string, is now
    the empty-command shape)."""
    with pytest.raises(HookConfigError, match="must not be an empty list"):
        load_hooks([{"on": "session_end", "exec": []}])


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
                {"on": "bad_point", "exec": ["echo"]},
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
        {"on": "session_end", "exec": ["echo", "a"]},
        {"on": "turn_end", "template_push": {"message": "second"}},
        {"on": "turn_end", "exec": ["echo", "b"]},
    ]
    registry = load_hooks(raw)
    hooks = registry.hooks_for("turn_end")
    # Exactly three hooks at turn_end — use unpack-enforcement so extra/missing fails
    first, second, third = hooks
    # Order: first push → second push → exec
    assert first.template_push is not None and first.template_push.message == "first"
    assert second.template_push is not None and second.template_push.message == "second"
    assert third.exec == ("echo", "b")


def test_registry_hooks_for_unknown_point_returns_empty() -> None:
    """Tier 1: ``hooks_for`` with an unknown point returns an empty list (no error)."""
    raw = [{"on": "turn_end", "exec": ["echo", "hi"]}]
    registry = load_hooks(raw)
    assert registry.hooks_for("agent_start") == []


def test_registry_hooks_for_no_match_returns_empty() -> None:
    """Tier 1: ``hooks_for`` returns an empty list when no hooks match the point."""
    raw = [{"on": "turn_end", "exec": ["echo", "hi"]}]
    registry = load_hooks(raw)
    assert registry.hooks_for("session_start") == []


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
  - on: turn_end
    template_push:
      message: "turn {{ chain_id }} done"
      wake: false
      push_when: "{{ ctx.should_notify }}"
      session: "{{ ctx.target_session }}"
    matcher:
      chain_id: turn-done-filter

  - on: session_start
    exec: ["scripts/on-session-start.sh"]
    matcher:
      agent_name: session-filter
""".lstrip()

    reyn_yaml = tmp_path / "reyn.yaml"
    reyn_yaml.write_text(yaml_content, encoding="utf-8")

    raw_cfg = yaml.safe_load(reyn_yaml.read_text(encoding="utf-8"))
    registry = load_hooks(raw_cfg.get("hooks"))

    # ── Hook 1: push hook at turn_end ────────────────────────────────────
    turn_end_hooks = registry.hooks_for("turn_end")
    (h1,) = turn_end_hooks  # exactly one — unpack enforces count
    assert h1.on == "turn_end"
    assert h1.template_push is not None
    assert h1.template_push.message == "turn {{ chain_id }} done"
    # non-default wake=False (default is True)
    assert h1.template_push.wake is False
    # non-default push_when template (default is "true")
    assert h1.template_push.push_when == "{{ ctx.should_notify }}"
    # non-default session template (default is None)
    assert h1.template_push.session == "{{ ctx.target_session }}"
    # non-default matcher (default is None) — schema-valid field for turn_end
    assert h1.matcher == {"chain_id": "turn-done-filter"}

    # ── Hook 2: exec hook at session_start ───────────────────────────────
    session_start_hooks = registry.hooks_for("session_start")
    (h2,) = session_start_hooks  # exactly one — unpack enforces count
    assert h2.on == "session_start"
    assert h2.exec == ("scripts/on-session-start.sh",)
    assert h2.template_push is None
    assert h2.matcher == {"agent_name": "session-filter"}

    # ── Hooks at other points are empty (no stray registrations) ─────────
    assert registry.hooks_for("session_end") == []
    assert registry.hooks_for("turn_start") == []


# ---------------------------------------------------------------------------
# #4517 — unquoted `on:` parses as the boolean True key (YAML 1.1 / PyYAML)
# ---------------------------------------------------------------------------


def test_on_key_from_real_unquoted_yaml_still_loads_via_the_true_key_fallback():
    """Tier 1: #4517's own reproduction, through REAL yaml.safe_load — an
    unquoted ``on: session_start`` parses to a ``True`` key (PyYAML is a
    YAML 1.1 implementation; `on`/`off`/`yes`/`no` are boolean literals
    there), and the loader's existing fallback (`raw.get("on",
    raw.get(True))`) still resolves it correctly. This was previously
    UNTESTED despite being real, invoked code — a genuine coverage gap
    the #4517 investigation surfaced, not a new behavior."""
    import yaml

    raw = yaml.safe_load(
        "hooks:\n  - on: session_start\n    exec: [echo, hi]\n"
    )
    assert True in raw["hooks"][0], "the fixture itself must reproduce the True-key shape"
    registry = load_hooks(raw["hooks"])
    (hd,) = registry.hooks_for("session_start")
    assert hd.exec == ("echo", "hi")


def test_true_key_hook_entry_logs_a_warning(caplog):
    """Tier 2: #4517 — a hook entry with a `True` key (the unquoted `on:`
    trap) logs a warning naming the entry index, even though the hook
    still loads correctly via the fallback. Zero false positives by
    construction: a `True` key has exactly one cause in a hand-authored
    hook entry (an unquoted `on:`/`off:`/`yes:`/`no:` bareword) — there is
    no other field whose YAML spelling produces a literal `True` key."""
    import logging

    with caplog.at_level(logging.WARNING, logger="reyn.hooks.loader"):
        load_hooks([{True: "session_start", "exec": ["echo", "hi"]}])
    assert any(
        "hooks[0]" in r.message and "True" in r.message for r in caplog.records
    ), f"expected a warning naming the True-key trap, got: {[r.message for r in caplog.records]}"


def test_true_key_warning_never_claims_the_hook_failed(caplog):
    """Tier 2: #4517 — architect's post-reversal ruling, condition 1 —
    the warning must never say the hook is broken or "NOT APPLIED"; it
    DID register and WILL fire. Saying otherwise would repeat #4515's
    own just-fixed false-report shape (asserting a failure that never
    happened) — the opposite mistake in the opposite direction."""
    import logging

    with caplog.at_level(logging.WARNING, logger="reyn.hooks.loader"):
        load_hooks([{True: "session_start", "exec": ["echo", "hi"]}])
    (record,) = [r for r in caplog.records if "hooks[0]" in r.message]
    lowered = record.message.lower()
    assert "not applied" not in lowered
    assert "broken" not in lowered
    assert "applied" in lowered, "must affirmatively say the hook DID apply"


def test_true_key_warning_names_the_pre_4519_docs(caplog):
    """Tier 1: #4517 — lead-coder's accepted-cost condition (④) — the
    warning names that older reyn docs (pre-#4519) taught this exact
    unquoted spelling, so an operator who copied it did nothing wrong."""
    import logging

    with caplog.at_level(logging.WARNING, logger="reyn.hooks.loader"):
        load_hooks([{True: "session_start", "exec": ["echo", "hi"]}])
    (record,) = [r for r in caplog.records if "hooks[0]" in r.message]
    assert "4519" in record.message or "docs" in record.message.lower()


def test_quoted_on_key_never_logs_the_warning(caplog):
    """Tier 2: accept-side — an operator who correctly quoted `"on":`
    never sees this warning (it fires only when the string key is
    ABSENT). Same discipline #4515's own accept-side test established:
    a false positive here would train operators to ignore the warning."""
    import logging

    with caplog.at_level(logging.WARNING, logger="reyn.hooks.loader"):
        load_hooks([{"on": "session_start", "exec": ["echo", "hi"]}])
    assert not any("True" in r.message for r in caplog.records), (
        f"a correctly-quoted 'on:' must never trigger the True-key warning, "
        f"got: {[r.message for r in caplog.records]}"
    )


def test_a_hook_entry_with_neither_on_nor_true_key_still_raises_not_warns():
    """Tier 1: regression guard — a genuinely missing `on` (no string key,
    no True key either) still raises HookConfigError as before; the new
    warning path only intercepts the SPECIFIC True-key shape, it does not
    change the missing-on error path."""
    with pytest.raises(HookConfigError, match="\\.on is required"):
        load_hooks([{"exec": ["echo", "hi"]}])


# ===========================================================================
# #4501 — unknown hook-entry keys are eager-rejected, not silently dropped
# ===========================================================================


def test_load_hooks_unknown_key_rejected() -> None:
    """Tier 1: a hook entry key outside the known vocabulary raises
    HookConfigError naming the key (#4501 — every individual field was
    already type-checked strictly; the entry's own key SET was not, so a
    typo'd key was silently dropped)."""
    with pytest.raises(HookConfigError, match="unrecognized key"):
        load_hooks([{"on": "turn_end", "exec": ["echo", "hi"], "nam": "typo"}])


def test_load_hooks_allow_write_paths_wrong_scope_gets_a_specific_hint() -> None:
    """Tier 1: `allow_write_paths` (the agent-level sandbox.policy field
    name, HOOK_SANDBOX_SCOPE's own left-hand column) written at a hook
    site raises HookConfigError naming the CORRECT per-hook key
    (`write_paths`) directly — the concrete case architect named in #4501
    (a real 3-hour incident: the wrong-scope name is the RIGHT name on the
    other side of the boundary, so it isn't a typo a spellchecker-shaped
    heuristic would catch)."""
    with pytest.raises(HookConfigError, match="allow_write_paths.*write_paths"):
        load_hooks(
            [{"on": "turn_end", "exec": ["echo", "hi"], "allow_write_paths": ["/tmp"]}]
        )


def test_load_hooks_every_known_key_together_still_accepts() -> None:
    """Tier 1: accept-side — a hook entry using every known key at once is
    NOT rejected by the new eager-reject check — the deny-side tests above
    only prove unknown keys ARE rejected, this proves the known-key set
    itself is complete and doesn't false-positive on a legitimate entry."""
    reg = load_hooks(
        [
            {
                "on": "turn_end",
                "name": "full-entry",
                "exec": ["echo", "hi"],
                "matcher": {"agent_name": "default"},
                "subprocess": True,
                "network": False,
                "write_paths": ["/tmp"],
            }
        ]
    )
    assert reg.hooks_for("turn_end")[0].name == "full-entry"


def test_load_hooks_quoted_on_key_is_not_flagged_as_unknown() -> None:
    """Tier 1: regression guard — the quoted `"on"` string key (the
    canonical, recommended spelling per #4519) must never itself be
    reported as an unrecognized key by the new #4501 check."""
    reg = load_hooks([{"on": "turn_end", "exec": ["echo", "hi"]}])
    assert reg.hooks_for("turn_end")[0].on == "turn_end"


def test_load_hooks_bareword_off_alongside_a_string_typo_raises_not_crashes() -> None:
    """Tier 1: lead-coder's #4526 review block, through REAL yaml.safe_load
    — a bareword `off:`/`no:` key (PyYAML/YAML 1.1 parses those as the
    boolean False, #4517's own sibling class) alongside an unrelated
    string-key typo used to raise an UNCAUGHT TypeError
    (`sorted([False, "typo"])`: '<' not supported between str and bool)
    instead of the friendly HookConfigError this whole check exists to
    produce — the crash-side mirror of the "silently dropped" defect
    class. Falsify-verified against the pre-fix `sorted(...)` (no
    `key=repr`): TypeError, not HookConfigError."""
    import yaml

    raw = yaml.safe_load(
        "hooks:\n  - \"on\": turn_end\n    exec: [echo, hi]\n"
        "    off: a\n    nam: typo\n"
    )
    assert False in raw["hooks"][0], "the fixture itself must reproduce the False-key shape"
    with pytest.raises(HookConfigError, match="unrecognized key"):
        load_hooks(raw["hooks"])
