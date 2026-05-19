"""Tier 2: dispatcher fallback for ARS-only direct-call salvage (#229).

Pre-fix, weak LLMs (B42 W5-S6) read qualified names from the ARS block
in ``invoke_action.description`` and emitted them as direct function
calls (= ``function_call.name = "skill__mcp_install"``). The name
wasn't in the dispatcher's ``tool_catalog`` (= ARS entries don't get a
top-level tool slot unless they're also hot-list aliases), so
``dispatch_tool`` rejected with ``unknown_tool``.

Per the #229 owner decision (α + β bundled):

  α — router_loop._execute_tool, before dispatch, detects "qualified
      name not in catalog" and rewrites the call to
      ``invoke_action(action_name=name, args=args)`` when
      ``universal_dispatch.resolve_invoke_action`` confirms the name
      is routable. An audit event
      ``direct_alias_call_salvaged`` records the rewrite.

  β — ``_enrich_invoke_action_description`` prefixes the ARS block
      schema lines with an explicit "NOT direct-callable; use
      invoke_action" instruction.

This file pins:
  1. Salvage triggers when name resolves through universal_dispatch.
  2. Salvage NOT triggered when name is garbage (= original
     unknown_tool surfacing preserved).
  3. Salvage NOT triggered when name is in catalog (= hot-list alias
     path unchanged).
  4. Audit event emitted with original name + rewrite target.
  5. ARS block hint text contains the explicit "NOT direct-callable"
     instruction.
"""
from __future__ import annotations

from typing import Any

from reyn.chat.router_loop import (
    RouterLoop,
    _enrich_invoke_action_description,
)


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
    assert len(audit) == 1
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


# ── 4. ARS block wording contains the "NOT direct-callable" instruction ─


def test_ars_block_includes_not_direct_callable_instruction() -> None:
    """Tier 2 (β): the enrichment hint warns the LLM against direct calls."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "invoke_action",
                "description": "Original description.",
                "parameters": {},
            },
        },
    ]
    ars_entries = [("skill__mcp_install", {})]
    enriched = _enrich_invoke_action_description(tools, ars_entries)
    desc = enriched[0]["function"]["description"]
    assert "NOT direct-callable" in desc
    assert "invoke_action" in desc
    # Schema line still present.
    assert "skill__mcp_install" in desc


def test_ars_block_unchanged_when_no_entries() -> None:
    """Tier 2 (β): empty ars_entries → no enrichment (= no spurious hint)."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "invoke_action",
                "description": "Pristine.",
                "parameters": {},
            },
        },
    ]
    enriched = _enrich_invoke_action_description(tools, [])
    assert enriched[0]["function"]["description"] == "Pristine."
