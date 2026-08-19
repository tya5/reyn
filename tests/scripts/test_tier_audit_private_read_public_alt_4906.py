"""Tier 1: #4906 — 2 false-negative forms in Rule 8 (#4864/#4905)'s gate.

tui-coder's own #4905 PR body disclosed a real restriction verbatim: the
attribute BASE is a bare ``ast.Name`` only, and ``_infer_local_types``'s
Assign branch only binds a single-``Name`` target. Both restrictions are
real corpus false negatives — architect reproduced them live against the
merged gate (a synthetic ``class_index``, zero findings, despite a genuine
violation):

  ① tuple-unpack: ``a, b = _make_pair()`` where ``_make_pair() -> tuple[A,
     B]`` — the Assign branch required ``len(stmt.targets) == 1 and
     isinstance(stmt.targets[0], ast.Name)``, so an ``ast.Tuple`` target
     was skipped entirely; ``a``'s type had zero evidence afterward.
  ② chained access: ``w.f._router_host`` — the attribute-base check was
     ``isinstance(base, ast.Name)``, excluding ``w.f`` (an ``ast.Attribute``
     chain) even when ``f`` is itself a ``@property`` returning a tracked
     class.

Both fixes stay evidence-only (never a guessed binding) and class-scoped
(never a bare attribute-name match) — the same zero-false-positive bar
#4864's own design set. This file pins both fixes AND their own negative
controls (an unresolvable link anywhere must still yield nothing), mirroring
the sibling ``test_tier_audit_private_read_public_alt_4864.py``'s structure.

Tier 1 because this is the audit script's own contract surface.
"""
from __future__ import annotations

import ast
import importlib.util

import pytest

from tests._support.paths import REPO_ROOT


def _load_audit_module():
    import sys
    script = REPO_ROOT / "scripts" / "test_tier_audit.py"
    spec = importlib.util.spec_from_file_location("_audit_tier_audit_script_4906", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit_mod():
    return _load_audit_module()


def _findings_for(
    audit_mod, source: str, class_index: dict, property_return_types: dict | None = None,
) -> list:
    tree = ast.parse(source)
    return audit_mod._check_private_read_with_public_alt(
        source, tree, class_index, property_return_types,
    )


# ── ① tuple-unpack: positive cases ──────────────────────────────────────────


def test_tuple_unpack_from_a_typed_factory_fires(audit_mod) -> None:
    """Tier 1: ``a, b = _make_pair()`` where ``_make_pair() -> tuple[Foo,
    Bar]`` binds ``a -> Foo`` positionally — the exact shape architect
    reproduced live (``a, b = _make_pair()`` then ``a._router_host``,
    zero findings pre-fix)."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    src = (
        'def _make_pair() -> "tuple[Foo, Bar]":\n'
        "    return Foo(), Bar()\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    a, b = _make_pair()\n"
        "    assert a._router_host is not None\n"
    )
    (only,) = _findings_for(audit_mod, src, class_index)
    assert "_router_host" in only.message
    assert "Foo.router_host" in only.message


def test_tuple_unpack_second_position_also_binds(audit_mod) -> None:
    """Tier 1: positional binding is not accidentally first-element-only —
    the SECOND unpacked name resolves too."""
    class_index = {"Bar": ({"_state_log"}, "src/bar.py")}
    src = (
        'def _make_pair() -> "tuple[Foo, Bar]":\n'
        "    return Foo(), Bar()\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    a, b = _make_pair()\n"
        "    assert b._state_log is not None\n"
    )
    (only,) = _findings_for(audit_mod, src, class_index)
    assert "_state_log" in only.message


def test_tuple_unpack_via_await_still_binds(audit_mod) -> None:
    """Tier 1: ``a, b = await _make_pair()`` — the ``ast.Await`` unwrap
    already covered the single-Name case; must still apply to tuple
    targets."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    src = (
        'async def _make_pair() -> "tuple[Foo, Bar]":\n'
        "    return Foo(), Bar()\n"
        "\n"
        "async def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    a, b = await _make_pair()\n"
        "    assert a._router_host is not None\n"
    )
    (only,) = _findings_for(audit_mod, src, class_index)
    assert "_router_host" in only.message


# ── ① tuple-unpack: negative cases (must NOT fire / must NOT crash) ────────


def test_tuple_unpack_arity_mismatch_does_not_bind(audit_mod) -> None:
    """Tier 1: 2 targets against a 3-element tuple annotation — never a
    partial/guessed bind. Must not fire (and must not raise)."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    src = (
        'def _make_triple() -> "tuple[Foo, Bar, Baz]":\n'
        "    return Foo(), Bar(), Baz()\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    a, b = _make_triple()\n"
        "    assert a._router_host is not None\n"
    )
    findings = _findings_for(audit_mod, src, class_index)
    assert findings == []


def test_tuple_unpack_with_starred_target_does_not_bind(audit_mod) -> None:
    """Tier 1: ``a, *rest = _make_pair()`` — a ``Starred`` element in the
    target skips the whole assignment, never a partial bind for the
    non-starred names."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    src = (
        'def _make_pair() -> "tuple[Foo, Bar]":\n'
        "    return Foo(), Bar()\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    a, *rest = _make_pair()\n"
        "    assert a._router_host is not None\n"
    )
    findings = _findings_for(audit_mod, src, class_index)
    assert findings == []


def test_tuple_unpack_from_an_untyped_factory_does_not_fire(audit_mod) -> None:
    """Tier 1: ``a, b = _make_pair()`` with NO return annotation at all
    stays unresolvable — never a guess from the unpacking shape alone."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    src = (
        "def _make_pair():\n"
        "    return Foo(), Bar()\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    a, b = _make_pair()\n"
        "    assert a._router_host is not None\n"
    )
    findings = _findings_for(audit_mod, src, class_index)
    assert findings == []


# ── ② chained access: positive case ─────────────────────────────────────────


def test_chained_property_access_fires(audit_mod) -> None:
    """Tier 1: ``w.f._router_host`` — ``f`` is a ``@property`` on
    ``Widget`` returning ``Foo`` (evidenced via ``property_return_types``);
    ``w``'s own type comes from its parameter annotation. The exact shape
    #4905's own PR body disclosed as out of scope (base restricted to a
    bare Name) and architect reproduced live."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    property_return_types = {("Widget", "f"): "Foo"}
    src = (
        "def test_thing(w: Widget):\n"
        '    """Tier 2: example."""\n'
        "    x = w.f._router_host\n"
        "    assert x is not None\n"
    )
    (only,) = _findings_for(audit_mod, src, class_index, property_return_types)
    assert "_router_host" in only.message
    assert "Foo.router_host" in only.message


def test_two_link_chain_also_resolves(audit_mod) -> None:
    """Tier 1: recursion isn't accidentally depth-1-only — ``w.g.f.
    _router_host`` (2 property links before the private read) still
    resolves when EVERY link has evidence."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    property_return_types = {
        ("Widget", "g"): "Gadget",
        ("Gadget", "f"): "Foo",
    }
    src = (
        "def test_thing(w: Widget):\n"
        '    """Tier 2: example."""\n'
        "    x = w.g.f._router_host\n"
        "    assert x is not None\n"
    )
    (only,) = _findings_for(audit_mod, src, class_index, property_return_types)
    assert "_router_host" in only.message


# ── ② chained access: negative cases (same-class-scoping, unresolvable) ────


def test_chained_access_with_no_return_type_evidence_does_not_fire(audit_mod) -> None:
    """Tier 1: ``w.f._router_host`` where ``f``'s return type is NOT in
    ``property_return_types`` (no evidence) — must not fire; the chain
    resolver returns None for the whole expression, never a guess."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    src = (
        "def test_thing(w: Widget):\n"
        '    """Tier 2: example."""\n'
        "    x = w.f._router_host\n"
        "    assert x is not None\n"
    )
    findings = _findings_for(audit_mod, src, class_index, {})
    assert findings == []


def test_chained_access_resolves_to_an_unbacked_class_does_not_fire(audit_mod) -> None:
    """Tier 1: architect's own #4864 pitfall, replayed through the chain
    path — ``w.f`` resolves to a REAL class (``Gadget``), but ``Gadget``
    has no backed private attr matching ``._router_host`` (it's backed
    only on the unrelated ``Foo``). Same-class scoping must hold for the
    chain resolver too, not just the direct-Name path."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    property_return_types = {("Widget", "f"): "Gadget"}
    src = (
        "def test_thing(w: Widget):\n"
        '    """Tier 2: example."""\n'
        "    x = w.f._router_host\n"
        "    assert x is not None\n"
    )
    findings = _findings_for(audit_mod, src, class_index, property_return_types)
    assert findings == []


def test_self_base_still_excluded_through_the_new_resolver(audit_mod) -> None:
    """Tier 1: regression sanity — ``self._x`` inside a method must still
    never fire (deliberately out of scope), now that the base check no
    longer short-circuits on ``isinstance(base, ast.Name)`` first."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    src = (
        "class Foo:\n"
        "    def test_thing(self):\n"
        '        """Tier 2: example."""\n'
        "        assert self._router_host is not None\n"
    )
    findings = _findings_for(audit_mod, src, class_index)
    assert findings == []
