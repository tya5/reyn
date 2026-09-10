"""Tier 2: #6083 ⑵-a — the control-POST read timeout applies by CLASS, not
per-payload-type guesswork: a payload type whose server-side handler awaits
an operation with genuinely unbounded duration is exempt (unbounded read,
matching the SSE stream's own policy); every other one stays on the bounded
10s default #5894 established.

★ Population is DERIVED, never hand-listed. ``protocol.py``'s own
``LONG_RUNNING_PAYLOAD_TYPES`` / ``BOUNDED_PAYLOAD_TYPES`` are the single
declared classification — architect co-vet (PR #6105): this is a fact BOTH
ends of the wire need (how long a payload's own POST can legitimately take),
not one side's implementation detail, so it lives in the wire-protocol codec
module both ends already import, not in ``endpoint.py`` (the server's own
implementation) where it was first drafted. This module's own
:func:`test_every_ptype_agui_submit_branches_on_is_classified` is what keeps
that declaration from silently falling behind a NEW branch: it AST-walks
``endpoint.agui_submit``'s real source (the population is still derived from
the SERVER dispatch's own structure — only the classification's location
moved) for every literal ``ptype ==`` comparison and asserts each one is
classified on exactly one side. A new branch nobody classified is a
DERIVATION GAP this test fails on, not a name someone forgot to add to a
list they never look at again.

The decision itself (:func:`reyn.interfaces.repl.remote_client._read_timeout_
for`) is tested as a PURE FUNCTION — no live socket, no wait — per
testing.md's own guidance: a duration is rarely the property under test, and
splitting the decision out as a pure function is what removes the place a
duration could be written into a test at all. The real socket-level mechanics
(does ``httpx.Timeout(None, ...)`` genuinely never time out) are already
proven by ``test_5894_control_timeout_and_coalesce.py``'s own real-listener
tests; this module does not re-prove that.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from reyn.interfaces.repl import remote_client as remote_client_mod
from reyn.interfaces.repl.remote_client import _CONTROL_TIMEOUT_S, _read_timeout_for
from reyn.interfaces.transport.agui.endpoint import agui_submit
from reyn.interfaces.transport.agui.protocol import (
    BOUNDED_PAYLOAD_TYPES,
    LONG_RUNNING_PAYLOAD_TYPES,
)


def _ptypes_agui_submit_branches_on() -> "set[str]":
    """AST-derive every literal string ``agui_submit`` compares ``ptype``
    against (``if``/``elif ptype == "..."``, either operand order) — the
    REAL population the classification below must fully cover. Never a
    hand-typed mirror of the branches: this walks the function's own
    current source, so a new branch is picked up automatically the next
    time this runs.

    ⚠️ Scope (lead-coder BLOCKING, PR #6105): this walk recognizes exactly
    ONE shape — an ``ast.Compare`` with a single ``Eq`` op, comparing a
    bare ``ptype`` ``Name`` against a string ``Constant`` (either operand
    order). It does NOT see ``ptype in (...)``, a ``match`` statement, or a
    dict-dispatch table — a future branch written in one of those forms
    would not be found here, so :func:`test_every_ptype_agui_submit_
    branches_on_is_classified` would stay green while actually missing a
    real, unclassified branch. This is a scope limit of THIS derivation,
    not something the test's own vacuity guard
    (:func:`test_the_ast_derivation_itself_is_not_vacuous`) catches —
    that guard only floors on the two ``ptype``s already known to exist
    today, so a new branch in one of the unrecognized forms would not
    trip it either. Deliberately not widened: matching more shapes here
    would mean guessing every form a future branch might take, which
    would just replace the AST walk with a second hand-maintained list.
    """
    source = inspect.getsource(agui_submit)
    tree = ast.parse(source)
    found: "set[str]" = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)):
            continue
        left = node.left
        right = node.comparators[0]
        for name_side, other_side in ((left, right), (right, left)):
            if (isinstance(name_side, ast.Name) and name_side.id == "ptype"
                    and isinstance(other_side, ast.Constant)
                    and isinstance(other_side.value, str)):
                found.add(other_side.value)
    return found


def test_the_ast_derivation_itself_is_not_vacuous() -> None:
    """Tier 2: question 4's own discipline — a derivation that silently
    found zero branches would make every assertion below pass by having
    nothing to check. Sanity floor: the two `ptype`s this module's own
    docstring names by name must actually be found."""
    found = _ptypes_agui_submit_branches_on()
    assert {"heartbeat", "attach_request"} <= found, (
        f"the AST walk found an implausibly small population -- it likely "
        f"stopped matching agui_submit's real branch shape: {found!r}"
    )


def test_every_ptype_agui_submit_branches_on_is_classified() -> None:
    """Tier 2: the derivation gate itself. A `ptype` `agui_submit` actually
    branches on that is classified on NEITHER side is exactly the shape
    lead-coder's own ruling forbids landing silently -- a future branch
    nobody decided the timeout class for."""
    found = _ptypes_agui_submit_branches_on()
    classified = LONG_RUNNING_PAYLOAD_TYPES | BOUNDED_PAYLOAD_TYPES
    unclassified = found - classified
    assert not unclassified, (
        f"agui_submit branches on {unclassified!r} with no timeout-class "
        f"decision recorded in protocol.py's LONG_RUNNING_PAYLOAD_TYPES/"
        f"BOUNDED_PAYLOAD_TYPES -- classify it on one side there (the "
        f"fact both ends of the wire need: does this payload's own POST "
        f"legitimately take a long time)."
    )


def test_the_two_sets_do_not_overlap() -> None:
    """Tier 2: deny side -- a `ptype` cannot be both bounded and
    long-running; an overlap would make `_read_timeout_for`'s own
    membership check ambiguous about which branch actually decided."""
    overlap = LONG_RUNNING_PAYLOAD_TYPES & BOUNDED_PAYLOAD_TYPES
    assert not overlap, f"classified on both sides: {overlap!r}"


def test_no_classified_type_is_stale() -> None:
    """Tier 2: the reverse direction of the derivation gate -- a classified
    `ptype` that `agui_submit` no longer actually branches on (a removed or
    renamed branch whose classification was left behind) is drift too,
    just not the silently-dangerous kind the other test blocks on."""
    found = _ptypes_agui_submit_branches_on()
    classified = LONG_RUNNING_PAYLOAD_TYPES | BOUNDED_PAYLOAD_TYPES
    stale = classified - found
    assert not stale, (
        f"classified but agui_submit no longer branches on it: {stale!r} "
        f"-- likely a renamed/removed ptype branch"
    )


def test_attach_request_gets_no_read_timeout() -> None:
    """Tier 1: attach_request is the one derived long-running type -- its
    own computed read timeout is None (unbounded), matching the SSE
    stream's own policy, never the bounded 10s control-POST default."""
    assert _read_timeout_for("attach_request") is None


@pytest.mark.parametrize("ptype", sorted(BOUNDED_PAYLOAD_TYPES))
def test_every_bounded_type_keeps_the_control_timeout(ptype: str) -> None:
    """Tier 1: non-vacuity's other half -- every OTHER classified type
    still gets the bounded default, not accidentally exempted."""
    assert _read_timeout_for(ptype) == _CONTROL_TIMEOUT_S


def test_an_unclassified_type_defaults_to_bounded() -> None:
    """Tier 1: fail-closed (lead-coder ruling, condition 2) -- a `ptype`
    this function has never seen is read as bounded (the side the timeout
    still applies to), never as long-running. The dangerous direction
    (unbounded by accident) would let a broken handler hang the client
    forever; this direction only ever cuts one short."""
    assert _read_timeout_for("__no_such_ptype__") == _CONTROL_TIMEOUT_S


def test_an_override_bounds_a_bounded_type_but_not_a_long_running_one() -> None:
    """Tier 1: `override` (the existing seam tests already use to inject
    T, #5894) still works for a bounded type; it has no effect on
    attach_request, which stays unbounded regardless -- there is no T to
    inject for a type this function has already decided has none."""
    assert _read_timeout_for("heartbeat", override=5.0) == 5.0
    assert _read_timeout_for("attach_request", override=5.0) is None


def test_strip_falsify_removing_attach_request_from_the_set_changes_the_decision() -> None:
    """Tier 2: strip-falsify, in-process (no git stash/checkout/restore,
    per lead-coder's own instruction) -- temporarily empty
    ``remote_client``'s own bound name for ``LONG_RUNNING_PAYLOAD_TYPES``
    (its module-level import of ``protocol.py``'s set -- the exact
    global ``_read_timeout_for`` resolves at call time, per that
    function's own docstring), confirm the decision flips to bounded,
    then restore. Proves this test module is actually exercising the
    production coupling, not a copy that could silently drift from it.
    """
    original = remote_client_mod.LONG_RUNNING_PAYLOAD_TYPES
    assert "attach_request" in original, "arrange: the real set must start non-empty"
    try:
        remote_client_mod.LONG_RUNNING_PAYLOAD_TYPES = frozenset()
        assert _read_timeout_for("attach_request") == _CONTROL_TIMEOUT_S, (
            "stripping attach_request out of the bound set did not change "
            "_read_timeout_for's own decision -- it is not actually reading "
            "remote_client's own module-level name"
        )
    finally:
        remote_client_mod.LONG_RUNNING_PAYLOAD_TYPES = original
    # Restored: the ordinary (non-stripped) behavior returns.
    assert _read_timeout_for("attach_request") is None
