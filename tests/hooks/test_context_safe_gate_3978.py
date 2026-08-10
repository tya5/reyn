"""Tier 2: proposal 0067 P2 — the context_safe gate + hook-push ``include``.

"Build the gate before adding the first field that needs it" (proposal §
"The gate, before the field that needs it") — today's 8 builtin schemas
mark every field safe (``CONTEXT_UNSAFE_FIELDS`` is empty, owner ruling
2026-08-10), so this file exercises the MECHANISM directly (via
``monkeypatch`` on the real module-level dict, not a fake collaborator —
the dict itself is the production data structure) rather than waiting for
a real unsafe field to exist.

Real ``PushBlock``/``render_push`` throughout — no mocks. Policy per
``docs/deep-dives/contributing/testing.md``.
"""
from __future__ import annotations

import pytest

from reyn.hooks import schema_registry
from reyn.hooks.render import render_push
from reyn.hooks.schema import PushBlock
from reyn.hooks.schema_registry import (
    BUILTIN_HOOK_SCHEMAS,
    CONTEXT_UNSAFE_FIELDS,
    safe_context_fields,
)

# ── CONTEXT_UNSAFE_FIELDS itself ────────────────────────────────────────────


def test_context_unsafe_fields_is_empty_for_the_original_8_builtin_kinds() -> None:
    """Tier 2: owner ruling (2026-08-10) — the 8 PRE-P3 builtin schemas'
    fields are all context_safe. The empty default for each of THESE 8 is
    the current real state, not a placeholder (see schema_registry.py's own
    module comment). ``task_settled`` (P3, added after this ruling) is
    the deliberate exception — see the next test."""
    pre_p3_kinds = frozenset(BUILTIN_HOOK_SCHEMAS) - {"builtin:task:task_settled"}
    for kind in pre_p3_kinds:
        assert CONTEXT_UNSAFE_FIELDS.get(kind, frozenset()) == frozenset()


def test_context_unsafe_fields_excludes_task_settled_result() -> None:
    """Tier 2: proposal 0067 P3 / ADR-0040 D3 — ``result`` is LLM-authored
    task output, declared context_safe: false in the design itself (not a
    narrowing added after the fact, unlike a hypothetical future field)."""
    assert CONTEXT_UNSAFE_FIELDS["builtin:task:task_settled"] == frozenset({"result"})


def test_context_unsafe_fields_only_names_real_schema_fields() -> None:
    """Tier 2: the sync-drift guard lead-coder asked for (broker 2026-08-10)
    — every field CONTEXT_UNSAFE_FIELDS names for a kind must actually be a
    member of that kind's own BUILTIN_HOOK_SCHEMAS field-set. A stale/typo'd
    entry here would otherwise silently do nothing (safe_context_fields only
    REMOVES a name it finds a match for) — the dangerous direction (an
    intended-unsafe field silently staying safe), closed mechanically rather
    than by convention."""
    for kind, unsafe in CONTEXT_UNSAFE_FIELDS.items():
        assert kind in BUILTIN_HOOK_SCHEMAS, (
            f"CONTEXT_UNSAFE_FIELDS names a kind {kind!r} with no builtin schema"
        )
        assert unsafe <= BUILTIN_HOOK_SCHEMAS[kind], (
            f"{kind}: CONTEXT_UNSAFE_FIELDS names a field not in its own schema "
            f"(extra={sorted(unsafe - BUILTIN_HOOK_SCHEMAS[kind])})"
        )


# ── safe_context_fields ──────────────────────────────────────────────────────


def test_safe_context_fields_with_no_entry_returns_context_unchanged() -> None:
    """Tier 2: a kind with no CONTEXT_UNSAFE_FIELDS entry (today: all 8
    builtins) removes nothing — the empty-deny-list default."""
    context = {"agent_name": "alpha", "chain_id": "c1"}
    assert safe_context_fields("turn_end", context) == context


def test_safe_context_fields_removes_only_the_named_unsafe_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: with an unsafe field declared (injected via monkeypatch on
    the real module dict — not a fake collaborator, the production data
    structure itself, temporarily populated for this test), that field is
    removed from the returned dict; every other field passes through."""
    monkeypatch.setitem(
        schema_registry.CONTEXT_UNSAFE_FIELDS,
        "builtin:lifecycle:turn_end",
        frozenset({"user_text"}),
    )
    context = {"agent_name": "alpha", "chain_id": "c1", "user_text": "secret"}

    filtered = safe_context_fields("turn_end", context)

    assert filtered == {"agent_name": "alpha", "chain_id": "c1"}


def test_safe_context_fields_accepts_bare_or_canonical_point_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: falsification pair for the accept path above — the bare
    ("turn_end") and canonical ("builtin:lifecycle:turn_end") spellings of
    the SAME point must filter identically, matching canonical_kind's own
    permanent-alias contract."""
    monkeypatch.setitem(
        schema_registry.CONTEXT_UNSAFE_FIELDS,
        "builtin:lifecycle:turn_end",
        frozenset({"user_text"}),
    )
    context = {"agent_name": "alpha", "user_text": "secret"}

    assert safe_context_fields("turn_end", context) == safe_context_fields(
        "builtin:lifecycle:turn_end", context
    )


# ── render_push's message gate ───────────────────────────────────────────────


def test_task_settled_result_cannot_be_interpolated_into_message() -> None:
    """Tier 2: (#3978 P3, lead-coder's 5th acceptance condition) the REAL
    production entry — not a monkeypatched injection — for task_settled's
    own CONTEXT_UNSAFE_FIELDS declaration. A message template referencing
    `result` (LLM-authored task output, ADR-0040 D3) is blocked by the
    gate that already exists for it in the shipped registry, no injection
    needed to prove it."""
    push = PushBlock(message="task finished: {{ result }}")

    result = render_push(push, {"result": "leak me"}, "task_settled")

    assert result.push_when is False
    assert result.message == ""


def test_render_push_message_cannot_interpolate_an_unsafe_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a message template referencing a field CONTEXT_UNSAFE_FIELDS
    excludes for this point hits the EXISTING StrictUndefined safety net —
    the SAME failure path a genuine typo would (no new error class), which
    render_push's own render-error safety net turns into push_when=False
    (skip, not crash)."""
    monkeypatch.setitem(
        schema_registry.CONTEXT_UNSAFE_FIELDS,
        "builtin:lifecycle:turn_end",
        frozenset({"user_text"}),
    )
    push = PushBlock(message="said: {{ user_text }}")

    result = render_push(push, {"user_text": "leak me"}, "turn_end")

    assert result.push_when is False
    assert result.message == ""


def test_render_push_wake_and_push_when_and_session_still_see_unsafe_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: falsification pair — the gate applies to ``message`` ONLY
    (proposal 0067 P2, module docstring). wake/push_when/session templates
    referencing the SAME field CONTEXT_UNSAFE_FIELDS excludes from message
    still render normally against the full context."""
    monkeypatch.setitem(
        schema_registry.CONTEXT_UNSAFE_FIELDS,
        "builtin:lifecycle:turn_end",
        frozenset({"user_text"}),
    )
    push = PushBlock(
        message="static, no reference",
        wake="{{ user_text }}",  # "leak me" is falsy-unrecognised... use a real bool string
        push_when="true",
        session="{{ user_text }}",
    )

    result = render_push(push, {"user_text": "on"}, "turn_end")

    assert result.push_when is True
    assert result.wake is True  # rendered "on" -> True, proves the field WAS visible here
    assert result.session == "on"


# ── PushBlock.include ─────────────────────────────────────────────────────────


def test_include_default_is_empty_and_changes_nothing() -> None:
    """Tier 2: accept-side non-vacuity — include=() (the default) produces
    byte-identical output to pre-P2 behaviour for every existing config."""
    push = PushBlock(message="hello")
    result = render_push(push, {}, "turn_end")
    assert result.message == "hello"


def test_include_appends_the_named_fields_raw_value_verbatim() -> None:
    """Tier 2: the field's raw value is appended after message, fenced and
    attributed by name."""
    push = PushBlock(message="see below", include=("chain_id",))
    result = render_push(push, {"chain_id": "abc-123"}, "turn_end")

    assert result.message.startswith("see below")
    assert "chain_id" in result.message
    assert "abc-123" in result.message


def test_include_does_not_pass_the_value_through_jinja2() -> None:
    """Tier 2: THE point of include — a value containing Jinja2 syntax is
    NOT evaluated. Falsification: if include rendered through Jinja2,
    '{{ 6*7 }}' would appear as '42'; it must appear literally instead."""
    push = PushBlock(message="payload attached", include=("user_text",))
    result = render_push(push, {"user_text": "{{ 6*7 }}"}, "turn_end")

    assert "{{ 6*7 }}" in result.message
    assert "42" not in result.message


def test_include_of_an_unsafe_field_still_carries_its_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the whole reason include exists (proposal 0067 P2 retraction
    §12: "context_safe: false ≠ hook can't carry content, only ≠ template
    interpolation") — a field excluded from MESSAGE interpolation still
    reaches the pushed text via include, since include never touches
    Jinja2 at all."""
    monkeypatch.setitem(
        schema_registry.CONTEXT_UNSAFE_FIELDS,
        "builtin:lifecycle:turn_end",
        frozenset({"user_text"}),
    )
    push = PushBlock(message="static", include=("user_text",))

    result = render_push(push, {"user_text": "the actual content"}, "turn_end")

    assert result.push_when is True  # message itself never referenced the unsafe field
    assert "the actual content" in result.message


def test_include_names_a_field_absent_from_context() -> None:
    """Tier 2: a typo'd/absent field name is surfaced as '(absent)' rather
    than silently omitted — a silent skip would misreport 'the operator
    asked for nothing', which is not what happened."""
    push = PushBlock(message="m", include=("does_not_exist",))
    result = render_push(push, {}, "turn_end")
    assert "(absent)" in result.message
