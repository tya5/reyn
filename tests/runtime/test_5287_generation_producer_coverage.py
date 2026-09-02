"""Tier 2: #5287 — every producer's own "which methods bump my generation"
set is DERIVED FROM THE CLASS's real source (AST), not a hand-typed list in
this test (#5228's own lesson: a hand-typed enumeration in the TEST can
silently drift from what the class actually does, the exact "declared but
not measured" shape this repo keeps closing). A vacuity guard (the derived
set must be non-empty) runs first, so an AST pattern that stopped matching
anything would fail LOUDLY here instead of this file quietly asserting
over an empty collection.

Each of the 3 producers #5287 gives a ``generation`` counter to —
``HookDispatcher`` (``src/reyn/hooks/dispatcher.py``), ``MCPConnectionService``
(``src/reyn/mcp/connection_service.py``), and ``Session`` itself (2 narrower
counters — ``_hook_toggle_generation``/``_capability_inputs_generation``,
not a class-wide ``generation`` since ``Session`` is not itself the sole
owner of a single cache's inputs) — bumps via one of 2 uniform markers:

  - a call to ``self._bump_generation()`` (``HookDispatcher``/
    ``MCPConnectionService``, each with their own private helper of that
    exact name — #5287 deliberately used the SAME marker shape across both
    so this scan needs one pattern, not one per class).
  - an augmented assignment ``self.<attr> += 1`` (``Session``'s 2 narrower
    counters — a full private-helper-per-counter felt like ceremony on an
    already very large class; the marker is the attribute name itself).

strip-falsify (recorded here, executed manually before landing): removing
any ONE of the enumerated bump lines from its production method (a) drops
that method's name from this file's own AST-derived set (the completeness
assertions below go red — the enumeration itself changed), and (b) makes
the matching functional test in this file (which calls the REAL method and
asserts the counter moved) go red independently. Both were verified by
hand: deleting ``MCPConnectionService._track_subscription``'s own
``self._bump_generation()`` line reproduced exactly this — the
completeness assert failed (the derived set no longer contained
``_track_subscription``) AND ``test_mcpconnectionservice_track_and_
untrack_subscription_bump`` failed independently (generation stayed put).

Functional coverage note (disclosed, not silently narrowed):
``MCPConnectionService._ensure_open``/``_reconnect`` are structurally
enumerated below (their own ``self._bump_generation()`` calls are real,
grep-confirmed AST hits) but NOT independently functionally witnessed in
THIS file — both require a genuine (re)connect, already exercised by
``tests/mcp/test_5280_mcp_reconnect_failed_event.py`` and #4686's own
suite against a real MCP subprocess; duplicating that harness here just to
re-prove "generation moved" was judged not worth the added real-subprocess
dependency for a file whose own job is the ENUMERATION witness, not
connection-lifecycle correctness (already covered elsewhere).
"""
from __future__ import annotations

import ast

import pytest

from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.registry import HookRegistry
from reyn.mcp.connection_service import MCPConnectionService
from tests._support.paths import REPO_ROOT

_DISPATCHER_PY = REPO_ROOT / "src" / "reyn" / "hooks" / "dispatcher.py"
_CONNECTION_SERVICE_PY = REPO_ROOT / "src" / "reyn" / "mcp" / "connection_service.py"
_SESSION_PY = REPO_ROOT / "src" / "reyn" / "runtime" / "session.py"


def _class_node(tree: ast.Module, class_name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"class {class_name!r} not found")


def _methods_calling(tree: ast.Module, class_name: str, call_name: str) -> "set[str]":
    """Every method of *class_name* whose body contains a call to
    ``self.<call_name>()`` — the ``self._bump_generation()`` marker."""
    cls = _class_node(tree, class_name)
    out: "set[str]" = set()
    for method in cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == call_name
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                out.add(method.name)
                break
    return out


def _methods_augassigning(tree: ast.Module, class_name: str, attr_name: str) -> "set[str]":
    """Every method of *class_name* whose body contains ``self.<attr_name> += ...``."""
    cls = _class_node(tree, class_name)
    out: "set[str]" = set()
    for method in cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.AugAssign)
                and isinstance(node.target, ast.Attribute)
                and node.target.attr == attr_name
                and isinstance(node.target.value, ast.Name)
                and node.target.value.id == "self"
            ):
                out.add(method.name)
                break
    return out


# ── HookDispatcher ───────────────────────────────────────────────────────


def test_hookdispatcher_bump_sites_are_derived_and_non_empty() -> None:
    """Tier 2: vacuity guard + completeness pin — the AST-derived set of
    ``HookDispatcher`` methods calling ``self._bump_generation()`` is
    non-empty AND matches the known real site exactly (a regression pin:
    a new mutation site added later without its own bump would silently
    NOT appear here, which is exactly what this pin exists to catch — see
    the module docstring's strip-falsify)."""
    tree = ast.parse(_DISPATCHER_PY.read_text(encoding="utf-8"))
    sites = _methods_calling(tree, "HookDispatcher", "_bump_generation")
    assert sites, "no HookDispatcher method calls self._bump_generation() at all"
    assert sites == {"replace_registry"}, sites


def test_hookdispatcher_replace_registry_bumps_generation() -> None:
    """Tier 2: functional witness for the one enumerated site above."""
    dispatcher = HookDispatcher(
        HookRegistry(defs=[]),
        put_inbox=lambda **kw: None,
        stage_next_turn_context=lambda **kw: None,
    )
    assert dispatcher.generation == 0
    dispatcher.replace_registry(HookRegistry(defs=[]))
    assert dispatcher.generation == 1
    dispatcher.replace_registry(HookRegistry(defs=[]))
    assert dispatcher.generation == 2


# ── MCPConnectionService ─────────────────────────────────────────────────


def test_mcpconnectionservice_bump_sites_are_derived_and_non_empty() -> None:
    """Tier 2: vacuity guard + completeness pin — see the module docstring
    for why ``_ensure_open``/``_reconnect`` are enumerated here (real AST
    hits) but not independently functionally witnessed in this file."""
    tree = ast.parse(_CONNECTION_SERVICE_PY.read_text(encoding="utf-8"))
    sites = _methods_calling(tree, "MCPConnectionService", "_bump_generation")
    assert sites, "no MCPConnectionService method calls self._bump_generation() at all"
    assert sites == {
        "_ensure_open", "_reconnect", "aclose",
        "_track_subscription", "_untrack_subscription", "_set_last_honored",
    }, sites


def test_mcpconnectionservice_track_and_untrack_subscription_bump() -> None:
    """Tier 2: functional witness for 2 of the enumerated sites above."""
    service = MCPConnectionService()
    assert service.generation == 0
    service._track_subscription("srv", "resource://x")
    assert service.generation == 1
    service._untrack_subscription("srv", "resource://x")
    assert service.generation == 2


def test_mcpconnectionservice_set_last_honored_bumps() -> None:
    """Tier 2: functional witness for the 3rd of the enumerated sites —
    the delegation point that replaced ``_HeldConnection``'s own former
    direct ``self._service._last_honored[...] = `` write (#5287)."""
    service = MCPConnectionService()
    assert service.generation == 0
    service._set_last_honored("srv", {"resource://x"})
    assert service.generation == 1
    assert service.unhonored_uris("srv") == []


@pytest.mark.asyncio
async def test_mcpconnectionservice_aclose_bumps() -> None:
    """Tier 2: functional witness for the 4th of the enumerated sites."""
    service = MCPConnectionService()
    gen_before = service.generation
    await service.aclose()
    assert service.generation == gen_before + 1


# ── Session's own 2 narrower generation counters ─────────────────────────


def test_session_hook_toggle_generation_bump_sites_are_derived_and_non_empty() -> None:
    """Tier 2: vacuity guard + completeness pin for ``Session``'s
    ``_hook_toggle_generation`` (the ``_disabled_hooks``-owning counter —
    see that field's own comment in ``Session.__init__``)."""
    tree = ast.parse(_SESSION_PY.read_text(encoding="utf-8"))
    sites = _methods_augassigning(tree, "Session", "_hook_toggle_generation")
    assert sites, "no Session method increments self._hook_toggle_generation at all"
    assert sites == {"set_hook_enabled", "load_persisted_toggles"}, sites


def test_session_capability_inputs_generation_bump_sites_are_derived_and_non_empty() -> None:
    """Tier 2: vacuity guard + completeness pin for ``Session``'s
    ``_capability_inputs_generation`` (the envelope-census counter — see
    that field's own comment in ``Session.__init__``)."""
    tree = ast.parse(_SESSION_PY.read_text(encoding="utf-8"))
    sites = _methods_augassigning(tree, "Session", "_capability_inputs_generation")
    assert sites, "no Session method increments self._capability_inputs_generation at all"
    assert sites == {"rekey_session_id", "refresh_mcp_servers", "_reapply_skills"}, sites
