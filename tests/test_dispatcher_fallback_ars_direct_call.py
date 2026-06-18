"""Tier 2: dispatcher fallback — qualified-name direct-call salvage (#229).

When the LLM emits a qualified action name (e.g. ``skill__mcp_install``,
``file__edit``) as a DIRECT function call instead of wrapping it in
``invoke_action(action_name=...)``, the name isn't in the dispatcher's
``tool_catalog`` (qualified names get no top-level tool slot unless they are
hot-list aliases), so ``dispatch_tool`` would reject with ``unknown_tool``.
The salvage (``router_loop._execute_tool`` →
``_maybe_salvage_qualified_direct_call``) detects "qualified name not in
catalog", confirms it routes via ``universal_dispatch.resolve_invoke_action``,
and rewrites the call to ``invoke_action(action_name=name, args=args)``. An
audit event ``direct_alias_call_salvaged`` records the rewrite.

#187 STEP 1c: this salvage is now MORE load-bearing. With the ARS block removed
from ``invoke_action.description`` (actions are enumerated only by
``list_actions``), a sibling-tool cross-ref pointer (e.g. file__write →
file__edit, #1420) leads the model to emit the pointed-at qualified name
directly; the salvage is what routes that emit to dispatch. The
pointer → direct-emit → salvage chain is the post-removal discovery path, so
these salvage invariants are part of STEP 1c's load-bearing surface.

This file pins (salvage standalone, no ARS dependency):
  1. Salvage triggers when the name resolves through universal_dispatch.
  2. Salvage NOT triggered when the name is garbage (= original unknown_tool
     surfacing preserved).
  3. Audit event emitted with original name + rewrite target.
"""
from __future__ import annotations

from typing import Any

from reyn.runtime.router_loop import RouterLoop


class _RecordingEvents:
    """Captures emit() calls in event order. Real shape, no mock."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, type: str, **data: Any) -> None:
        self.events.append((type, dict(data)))


class _MinimalRouterLoopShim:
    """Just enough RouterLoop surface to call _maybe_salvage_qualified_direct_call.

    The salvage method only consults ``self.host.events`` (for emit) and
    ``self.chain_id`` (for audit data). No registry, no LLM, no real
    catalog needed — the ``universal_dispatch.resolve_invoke_action``
    call consults the global default registry which is real.

    Bind the actual ``RouterLoop`` method so this shim exercises the
    same code path the production loop uses — no duplication.
    """

    chain_id = "test-chain"
    _maybe_salvage_qualified_direct_call = (
        RouterLoop._maybe_salvage_qualified_direct_call
    )

    def __init__(self) -> None:
        class _Host:
            events = _RecordingEvents()
        self.host = _Host()


# ── 1. Salvage triggers for valid qualified names ─────────────────────────


def test_salvage_rewrites_known_skill_to_invoke_action() -> None:
    """Tier 2: ``skill__mcp_install`` → ``invoke_action`` with action_name."""
    loop = _MinimalRouterLoopShim()
    new_name, new_args = loop._maybe_salvage_qualified_direct_call(
        "skill__mcp_install", {"server_id": "postgres"},
    )
    assert new_name == "invoke_action"
    assert new_args == {
        "action_name": "skill__mcp_install",
        "args": {"server_id": "postgres"},
    }


def test_salvage_rewrites_static_operation_to_invoke_action() -> None:
    """Tier 2: ``file__read`` (static op category) salvages too."""
    loop = _MinimalRouterLoopShim()
    new_name, new_args = loop._maybe_salvage_qualified_direct_call(
        "file__read", {"path": "README.md"},
    )
    assert new_name == "invoke_action"
    assert new_args["action_name"] == "file__read"


def test_salvage_with_empty_args_passes_empty_dict_through() -> None:
    """Tier 2: empty args dict survives the rewrite (= no None coercion)."""
    loop = _MinimalRouterLoopShim()
    new_name, new_args = loop._maybe_salvage_qualified_direct_call(
        "file__read", {},
    )
    assert new_name == "invoke_action"
    assert new_args["args"] == {}


# ── 2. Salvage skipped for garbage names ──────────────────────────────────


def test_salvage_returns_unchanged_for_unknown_qualified_name() -> None:
    """Tier 2: a name with __ but unresolvable → original (name, args).

    The dispatcher then surfaces the standard ``unknown_tool`` error,
    preserving error visibility for genuinely broken LLM emits.
    """
    loop = _MinimalRouterLoopShim()
    bad_name = "bogus_category__nonexistent"
    new_name, new_args = loop._maybe_salvage_qualified_direct_call(
        bad_name, {"x": 1},
    )
    assert new_name == bad_name
    assert new_args == {"x": 1}


def test_salvage_returns_unchanged_for_malformed_qualified_name() -> None:
    """Tier 2: a malformed name without category sep → unchanged."""
    loop = _MinimalRouterLoopShim()
    new_name, new_args = loop._maybe_salvage_qualified_direct_call(
        "__missing_category", {},
    )
    assert new_name == "__missing_category"


# ── 3. Audit event emitted on successful salvage ──────────────────────────


def test_salvage_emits_audit_event_on_rewrite() -> None:
    """Tier 2: ``direct_alias_call_salvaged`` records the rewrite."""
    loop = _MinimalRouterLoopShim()
    loop._maybe_salvage_qualified_direct_call(
        "file__read", {"path": "x"},
    )
    audit = [
        (t, d) for t, d in loop.host.events.events
        if t == "direct_alias_call_salvaged"
    ]
    assert audit, "expected at least one direct_alias_call_salvaged event"
    _, data = audit[0]
    assert data["original_name"] == "file__read"
    assert data["rewritten_to"] == "invoke_action"
    assert data["chain_id"] == "test-chain"


def test_salvage_does_not_emit_audit_on_unknown_name() -> None:
    """Tier 2: garbage name → no audit event (= rewrite never happened)."""
    loop = _MinimalRouterLoopShim()
    loop._maybe_salvage_qualified_direct_call(
        "garbage_cat__x", {},
    )
    assert all(
        t != "direct_alias_call_salvaged"
        for t, _ in loop.host.events.events
    )
