"""Tier 2: #5287 — every producer's own generation-bump coverage is
verified as ``mutators ⊆ bumpers``, both sets DERIVED FROM THE CLASS's
real source (AST), not hand-typed in this test (#5228's own lesson: a
hand-typed enumeration in the TEST can silently drift from what the class
actually does).

**Corrected mid-review (lead-coder BLOCK on #5676, head 6d6b27f95): the
FIRST version of this file asserted only ``bumpers == {known set}`` — the
set of methods CALLING ``self._bump_generation()``. That direction cannot
catch #5287's own actual defect class: a NEW method that mutates the
tracked state WITHOUT calling the bump helper never appears in ``bumpers``
at all, so ``bumpers == {known set}`` stays exactly as true (and green) as
before that method existed — it silently fails to grow. #5287's own
verbatim requirement was the OTHER direction: "WHOEVER ADDS A NEW SITE
THAT MUTATES ... nothing here will turn red on its own" — a forgotten
bump must turn something red.**

Fixed by deriving a SECOND, independent set — ``mutators`` — every method
whose body actually reassigns/mutates the tracked attribute(s), via a
DIFFERENT AST pattern (direct/annotated assignment, subscript-assignment,
or a call to a known in-place-mutating method — ``pop``/``clear``/
``discard``/``add``/``update``/``setdefault``/``remove``/etc. — whose
RECEIVER chain, however many ``.get(...)``/``.setdefault(...)`` deep,
roots at ``self.<attr>``; e.g. both ``self._subscriptions.setdefault(k,
set()).add(v)`` and ``self._subscriptions.get(k, set()).discard(v)``
count, because the object each actually mutates is the SAME instance
stored inside ``self._subscriptions``, never a copy) — then asserting
``mutators <= bumpers``. A future site that mutates the tracked state
without its own bump now genuinely widens ``mutators`` past ``bumpers``
and turns THIS assertion red, independent of what ``bumpers`` itself
contains.

**Naming fragility, disclosed (per lead-coder's own request — "どう名指
したか、なぜ壊れにくいかを1行"):** the tracked attribute is named by its
OWN field identifier (``_registry``, ``_clients``/``_subscriptions``/
``_subscription_adapters``/``_last_honored``, ``_disabled_hooks``,
``_available_skills``/``_session_id``/``_mcp_servers``) — a private field
that is itself the SAME name the production module's own #5287 comments
already reference (e.g. ``MCPConnectionService._bump_generation``'s own
docstring enumerates these 4 by name). A rename of the field would need
to touch every production read/write site too (the field would stop
existing under its old name), so this scanner breaking on a rename is the
CORRECT failure — it would break loudly (0 mutators found, tripping the
vacuity guard) rather than silently miscounting, not a fragile coincidence.

``__init__`` is excluded from the mutator scan for every producer: the
constructor's own INITIAL assignment never needs a bump (no cache exists
yet to go stale relative to it — the same reasoning
``HookDispatcher.generation``'s own docstring already gives, generalized
to every producer here).

strip-falsify, re-executed with the corrected direction (recorded here,
executed manually before landing): temporarily removed
``_track_subscription``'s own ``self._bump_generation()`` call — this
time ``mutators`` still (correctly) contains ``_track_subscription``
(the ``.setdefault(...).add(...)`` mutation is still there), but
``bumpers`` no longer does, so ``mutators <= bumpers`` fails loudly
(``{'_track_subscription'}`` is the reported extra element) — the exact
"forgot to bump" scenario #5287 exists to catch. Reverted before commit.
Also verified the FIRST version's own gap directly: with the SAME
mutation removed, the old ``bumpers == {known set WITHOUT
_track_subscription}`` assertion (re-derived, not the stale literal)
still passed — confirming the old direction really was blind to this.

Functional coverage note (disclosed, not silently narrowed):
``MCPConnectionService._ensure_open``/``_reconnect`` are structurally
enumerated below (their own ``self._bump_generation()`` calls are real,
grep-confirmed AST hits, and both are genuine ``mutators`` too — verified
by the subset assertion itself) but NOT independently functionally
witnessed in THIS file with a live call — both require a genuine
(re)connect, already exercised by
``tests/mcp/test_5280_mcp_reconnect_failed_event.py`` and #4686's own
suite against a real MCP subprocess; duplicating that harness here just
to re-prove "generation moved" was judged not worth the added
real-subprocess dependency for a file whose own job is the coverage
witness, not connection-lifecycle correctness (already covered
elsewhere).
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

#: In-place mutating method names this scan treats as "this call mutates
#: its receiver" — the vocabulary every #5287 mutation site in this repo
#: actually uses (dict/set primitives only; no custom mutator methods are
#: involved in any of the 3 producers' tracked fields).
_MUTATING_METHOD_NAMES = frozenset({
    "pop", "popitem", "clear", "discard", "add", "update",
    "setdefault", "remove", "extend", "append", "insert",
})

#: Never counted as a mutator needing its own bump — see the module
#: docstring's own paragraph on why.
_EXCLUDE_METHODS = frozenset({"__init__"})


def _class_node(tree: ast.Module, class_name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"class {class_name!r} not found")


def _methods_calling(tree: ast.Module, class_name: str, call_name: str) -> "set[str]":
    """Every method of *class_name* whose body contains a call to
    ``self.<call_name>()`` — the ``self._bump_generation()`` marker
    (``HookDispatcher``/``MCPConnectionService``)."""
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
    """Every method of *class_name* whose body contains ``self.<attr_name>
    += ...`` — the bump marker for ``Session``'s 2 narrower counters."""
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


def _chain_rooted_at_self_attr(node: ast.expr, attr_name: str) -> bool:
    """True if ``self.<attr_name>`` appears ANYWHERE inside *node* — used
    on a mutating call's own RECEIVER expression (``call.func.value``) so
    an arbitrarily-deep ``.get(...)``/``.setdefault(...)`` chain rooted at
    ``self.<attr_name>`` still counts (the object actually mutated by the
    outer call is the same instance stored inside that field, never a
    copy)."""
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Attribute)
            and n.attr == attr_name
            and isinstance(n.value, ast.Name)
            and n.value.id == "self"
        ):
            return True
    return False


def _methods_mutating(
    tree: ast.Module, class_name: str, attr_name: str,
) -> "set[str]":
    """Every method of *class_name* (excluding :data:`_EXCLUDE_METHODS`)
    that mutates ``self.<attr_name>`` — direct or annotated reassignment,
    a subscript-assignment rooted at it, or a call to a known in-place
    mutator (:data:`_MUTATING_METHOD_NAMES`) whose receiver chain roots
    at it. See the module docstring for the full pattern list and why."""
    cls = _class_node(tree, class_name)
    out: "set[str]" = set()
    for method in cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if method.name in _EXCLUDE_METHODS:
            continue
        hit = False
        for node in ast.walk(method):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign)
                    else [node.target]
                )
                for t in targets:
                    if (
                        isinstance(t, ast.Attribute) and t.attr == attr_name
                        and isinstance(t.value, ast.Name) and t.value.id == "self"
                    ):
                        hit = True
                    elif (
                        isinstance(t, ast.Subscript)
                        and _chain_rooted_at_self_attr(t.value, attr_name)
                    ):
                        hit = True
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MUTATING_METHOD_NAMES
                and _chain_rooted_at_self_attr(node.func.value, attr_name)
            ):
                hit = True
            if hit:
                break
        if hit:
            out.add(method.name)
    return out


# ── HookDispatcher ───────────────────────────────────────────────────────


def test_hookdispatcher_every_registry_mutator_bumps_generation() -> None:
    """Tier 2: the #5287 acceptance criterion itself — every method that
    reassigns ``self._registry`` (this class's own tracked field) must be
    among the methods calling ``self._bump_generation()``. A NEW mutator
    added later without its own bump widens ``mutators`` past ``bumpers``
    and fails HERE, independent of what ``bumpers`` itself contains."""
    tree = ast.parse(_DISPATCHER_PY.read_text(encoding="utf-8"))
    mutators = _methods_mutating(tree, "HookDispatcher", "_registry")
    bumpers = _methods_calling(tree, "HookDispatcher", "_bump_generation")
    assert mutators, "no HookDispatcher method mutates self._registry at all"
    assert mutators <= bumpers, (
        f"mutates _registry without bumping generation: {mutators - bumpers}"
    )
    # Regression pin (content, not the acceptance criterion itself -- see
    # module docstring for why this direction alone is insufficient).
    assert bumpers == {"replace_registry"}, bumpers


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

_MCP_TRACKED_FIELDS = ("_clients", "_subscriptions", "_subscription_adapters", "_last_honored")


def test_mcpconnectionservice_every_mutator_bumps_generation() -> None:
    """Tier 2: the #5287 acceptance criterion — every method mutating any
    of the 4 fields ``subscription_summary()`` composes from must be
    among the methods calling ``self._bump_generation()``."""
    tree = ast.parse(_CONNECTION_SERVICE_PY.read_text(encoding="utf-8"))
    mutators: "set[str]" = set()
    for field in _MCP_TRACKED_FIELDS:
        mutators |= _methods_mutating(tree, "MCPConnectionService", field)
    bumpers = _methods_calling(tree, "MCPConnectionService", "_bump_generation")
    assert mutators, "no MCPConnectionService method mutates any tracked field at all"
    assert mutators <= bumpers, (
        f"mutates tracked state without bumping generation: {mutators - bumpers}"
    )
    # Regression pin (content, not the acceptance criterion itself).
    assert bumpers == {
        "_ensure_open", "_reconnect", "aclose",
        "_track_subscription", "_untrack_subscription", "_set_last_honored",
    }, bumpers


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


def test_session_hook_toggle_generation_covers_every_disabled_hooks_mutator() -> None:
    """Tier 2: the #5287 acceptance criterion for ``Session``'s
    ``_hook_toggle_generation`` (the ``_disabled_hooks``-owning counter —
    see that field's own comment in ``Session.__init__``)."""
    tree = ast.parse(_SESSION_PY.read_text(encoding="utf-8"))
    mutators = _methods_mutating(tree, "Session", "_disabled_hooks")
    bumpers = _methods_augassigning(tree, "Session", "_hook_toggle_generation")
    assert mutators, "no Session method mutates self._disabled_hooks at all"
    assert mutators <= bumpers, (
        f"mutates _disabled_hooks without bumping _hook_toggle_generation: {mutators - bumpers}"
    )
    assert bumpers == {"set_hook_enabled", "load_persisted_toggles"}, bumpers


def test_session_capability_inputs_generation_covers_every_tracked_mutator() -> None:
    """Tier 2: the #5287 acceptance criterion for ``Session``'s
    ``_capability_inputs_generation`` (the envelope-census counter — see
    that field's own comment in ``Session.__init__``). Tracked fields:
    ``_available_skills`` (the skill roster), ``_session_id`` (a spawn-
    time re-key), ``_mcp_servers`` (Session's own copy of the MCP roster,
    reassigned in the SAME method as the ``RouterHostAdapter`` one it
    mirrors — see ``refresh_mcp_servers``'s own comment)."""
    tree = ast.parse(_SESSION_PY.read_text(encoding="utf-8"))
    mutators: "set[str]" = set()
    for field in ("_available_skills", "_session_id", "_mcp_servers"):
        mutators |= _methods_mutating(tree, "Session", field)
    bumpers = _methods_augassigning(tree, "Session", "_capability_inputs_generation")
    assert mutators, "no Session method mutates any tracked field at all"
    assert mutators <= bumpers, (
        f"mutates tracked state without bumping _capability_inputs_generation: {mutators - bumpers}"
    )
    assert bumpers == {"rekey_session_id", "refresh_mcp_servers", "_reapply_skills"}, bumpers
