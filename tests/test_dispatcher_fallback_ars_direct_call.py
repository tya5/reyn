"""Tier 2: dispatcher fallback — qualified-name direct-call salvage (#229).

When the LLM emits a qualified action name (e.g. ``mcp_install_registry``,
``edit_file``) as a DIRECT function call instead of wrapping it in
``invoke_action(action_name=...)``, the name isn't in the dispatcher's
``tool_catalog`` (qualified names get no top-level tool slot unless they are
hot-list aliases), so ``dispatch_tool`` would reject with ``unknown_tool``.
The salvage (``router_loop._execute_tool`` →
``_maybe_salvage_action_direct_call``) detects "qualified name not in
catalog", confirms it routes via ``universal_dispatch.resolve_invoke_action``,
and rewrites the call. An audit event ``direct_alias_call_salvaged`` records
the rewrite.

#3458: the rewrite target is the BARE spelling the alias resolves to whenever
that spelling is itself dispatchable, and ``invoke_action`` otherwise. The
wrapper is not advertised by every presentation, so an unconditional rewrite to
it dead-ended as ``unknown_tool: invoke_action`` under the schemes that omit it
— reachable as soon as the file tools started being advertised by default and
``without_duplicate_alias_spellings`` (#3428) dropped the redundant
``file__read`` / ``file__write`` aliases in favour of their bare names.

#187 STEP 1c: this salvage is now MORE load-bearing. With the ARS block removed
from ``invoke_action.description`` (actions are enumerated only by
``list_actions``), a sibling-tool cross-ref pointer (e.g. write_file →
edit_file, #1420) leads the model to emit the pointed-at qualified name
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
    """Just enough RouterLoop surface to call _maybe_salvage_action_direct_call.

    The salvage method only consults ``self.host.events`` (for emit),
    ``self.chain_id`` (for audit data), and the two catalog attributes. No
    registry, no LLM needed — the membership check reads the real, static
    catalog table.

    Bind the actual ``RouterLoop`` method so this shim exercises the
    same code path the production loop uses — no duplication.
    """

    chain_id = "test-chain"
    _maybe_salvage_action_direct_call = (
        RouterLoop._maybe_salvage_action_direct_call
    )

    def __init__(self, catalog: "dict | None" = None) -> None:
        # #3458/#3461: the salvage leaves a name alone when the executor can
        # already dispatch it, so the shim carries the two catalog attributes
        # the real RouterLoop has. An empty catalog = "not dispatchable under
        # its own name", which is the case the first section pins.
        self._catalog = catalog or {}
        self._dispatch_catalog = None

        class _Host:
            events = _RecordingEvents()
        self.host = _Host()


# ── 1. Salvage triggers for valid qualified names ─────────────────────────


def test_salvage_rewrites_known_action_to_invoke_action() -> None:
    """Tier 2: ``mcp_install_registry`` → ``invoke_action`` with action_name."""
    loop = _MinimalRouterLoopShim()
    new_name, new_args = loop._maybe_salvage_action_direct_call(
        "mcp_install_registry", {"server_id": "postgres"},
    )
    assert new_name == "invoke_action"
    assert new_args == {
        "action_name": "mcp_install_registry",
        "args": {"server_id": "postgres"},
    }


def test_salvage_rewrites_static_operation_to_invoke_action() -> None:
    """Tier 2: ``read_file`` (static op category) salvages too."""
    loop = _MinimalRouterLoopShim()
    new_name, new_args = loop._maybe_salvage_action_direct_call(
        "read_file", {"path": "README.md"},
    )
    assert new_name == "invoke_action"
    assert new_args["action_name"] == "read_file"


def test_salvage_with_empty_args_passes_empty_dict_through() -> None:
    """Tier 2: empty args dict survives the rewrite (= no None coercion)."""
    loop = _MinimalRouterLoopShim()
    new_name, new_args = loop._maybe_salvage_action_direct_call(
        "read_file", {},
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
    new_name, new_args = loop._maybe_salvage_action_direct_call(
        bad_name, {"x": 1},
    )
    assert new_name == bad_name
    assert new_args == {"x": 1}


def test_salvage_returns_unchanged_for_malformed_qualified_name() -> None:
    """Tier 2: a malformed name without category sep → unchanged."""
    loop = _MinimalRouterLoopShim()
    new_name, new_args = loop._maybe_salvage_action_direct_call(
        "__missing_category", {},
    )
    assert new_name == "__missing_category"


# ── 3. Audit event emitted on successful salvage ──────────────────────────


def test_salvage_emits_audit_event_on_rewrite() -> None:
    """Tier 2: ``direct_alias_call_salvaged`` records the rewrite."""
    loop = _MinimalRouterLoopShim()
    loop._maybe_salvage_action_direct_call(
        "read_file", {"path": "x"},
    )
    audit = [
        (t, d) for t, d in loop.host.events.events
        if t == "direct_alias_call_salvaged"
    ]
    assert audit, "expected at least one direct_alias_call_salvaged event"
    _, data = audit[0]
    assert data["original_name"] == "read_file"
    assert data["rewritten_to"] == "invoke_action"
    assert data["chain_id"] == "test-chain"


def test_salvage_does_not_emit_audit_on_unknown_name() -> None:
    """Tier 2: garbage name → no audit event (= rewrite never happened)."""
    loop = _MinimalRouterLoopShim()
    loop._maybe_salvage_action_direct_call(
        "garbage_cat__x", {},
    )
    assert all(
        t != "direct_alias_call_salvaged"
        for t, _ in loop.host.events.events
    )


# ── 4. #3458/#3461/#3429: a dispatchable name is left alone ───────────────


def test_salvage_leaves_a_dispatchable_name_alone() -> None:
    """Tier 2: #3458/#3461 — an action the executor can already dispatch is
    returned unchanged, not wrapped.

    #3461 added this arm as "if the alias's bare target is dispatchable,
    salvage to THAT" — because a qualified call whose bare equivalent was
    advertised would otherwise dead-end as ``unknown_tool: invoke_action``
    under a presentation that does not advertise the wrapper. #3429 removed
    the second spelling, so "the alias" and "its target" are one string and
    the arm degenerates into an early return — but the CONDITION still fires:
    CodeAct advertises nothing (``_catalog`` empty) and dispatches everything
    (``_dispatch_catalog`` full), so a name absent from the advertised catalog
    but present in the dispatchable one must reach the gate under its own
    name."""
    loop = _MinimalRouterLoopShim()
    loop._dispatch_catalog = {"read_file": object()}
    new_name, new_args = loop._maybe_salvage_action_direct_call(
        "read_file", {"path": "README.md"},
    )
    assert new_name == "read_file"
    assert new_args == {"path": "README.md"}


def test_salvage_emits_no_audit_when_it_left_the_name_alone() -> None:
    """Tier 2: #3429 — no ``direct_alias_call_salvaged`` for the early return.

    The event counts how often the model reached for a name the presentation
    did not give it a row for (#229). A name the executor dispatches under its
    own spelling is not that: nothing was rewritten, so nothing is recorded."""
    loop = _MinimalRouterLoopShim()
    loop._dispatch_catalog = {"read_file": object()}
    loop._maybe_salvage_action_direct_call("read_file", {"path": "x"})
    assert all(
        t != "direct_alias_call_salvaged"
        for t, _ in loop.host.events.events
    )
