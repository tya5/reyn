"""Tier 1: Rule 8 (#4864) — obj._x READ with a same-class public @property.

Pins the mechanism the #4864 thread specified: Rule 3 (private-state) only
walks ``ast.Assert`` test expressions, so the corpus's dominant evasion —
route the private read through a local var one line before the assert —
is invisible to it (126 sites, per the issue's own measurement). This rule
is a whole-file scan (#4900's own lesson: per-test walk drops 9/10 real
violations) that fires on ANY private read, not only inside ``assert``.

The class-scoped, type-evidence-gated design is the load-bearing part,
not incidental: architect's own #4864-thread pitfall is ``self._chains =
chains``, assigned in FIVE different classes in this repo, where only ONE
of them also defines ``@property def chains``. A global (does this attr
name exist as a property ANYWHERE) index would misfire on the other four.
These tests pin that a same-named-but-unbacked class does NOT cross-fire,
and that firing requires actual local type evidence (a param annotation
or a same-file factory/fixture def with a typed return) — never a bare
variable-name guess.

Tier 1 because this is the audit script's own contract surface, same
justification as ``test_tier_audit_private_state_ast.py``.
"""
from __future__ import annotations

import ast
import importlib.util

import pytest

from tests._support.paths import REPO_ROOT


def _load_audit_module():
    """Import ``scripts/test_tier_audit.py`` without pytest collecting it
    as a test module (same rationale as the sibling private-state test)."""
    import sys
    script = REPO_ROOT / "scripts" / "test_tier_audit.py"
    spec = importlib.util.spec_from_file_location("_audit_tier_audit_script_4864", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit_mod():
    return _load_audit_module()


def _findings_for(audit_mod, source: str, class_index: dict) -> list:
    tree = ast.parse(source)
    return audit_mod._check_private_read_with_public_alt(source, tree, class_index)


# ── Positive cases: detection MUST fire ─────────────────────────────────────


def test_factory_typed_return_makes_the_read_type_evident(audit_mod) -> None:
    """Tier 1: ``x = _make_foo()`` where ``_make_foo() -> Foo`` is a
    same-file def gives the rule its type evidence — the dominant
    corpus idiom (a local ``_make_x`` factory, not ``Foo(...)`` inline)."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    src = (
        "def _make_foo() -> Foo:\n"
        "    return Foo()\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    foo = _make_foo()\n"
        "    assert foo._router_host is not None\n"
    )
    (only,) = _findings_for(audit_mod, src, class_index)
    assert "_router_host" in only.message
    assert "Foo.router_host" in only.message


def test_param_annotation_makes_the_read_type_evident(audit_mod) -> None:
    """Tier 1: ``def test_x(foo: Foo)`` — annotation is direct evidence,
    no factory needed."""
    class_index = {"Foo": ({"_state_log"}, "src/foo.py")}
    src = (
        "def test_thing(foo: Foo):\n"
        '    """Tier 2: example."""\n'
        "    assert foo._state_log is None\n"
    )
    (only,) = _findings_for(audit_mod, src, class_index)
    assert "_state_log" in only.message


def test_quoted_union_annotation_makes_the_read_type_evident(audit_mod) -> None:
    """Tier 1: ``def f(x: "Foo | None")`` — a quoted forward-ref union.

    Not a hypothetical: this is the exact shape that made the FIRST
    version of this rule miss one of architect's own 4 named positive
    controls (#4864 thread) —
    ``def _op_ctx(tmp_path, session: "Session | None") -> OpContext:``
    in ``tests/core/test_2761_pr2_hotreload_immediate_apply.py:101``.
    ``_annotation_class_name`` originally treated the whole quoted string
    as one opaque candidate name (``"Session | None"``, matching nothing);
    fixed by re-parsing the string and unioning both branches."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    src = (
        "def test_thing(foo: \"Foo | None\"):\n"
        '    """Tier 2: example."""\n'
        "    assert (foo._router_host if foo is not None else None) is None\n"
    )
    (only,) = _findings_for(audit_mod, src, class_index)
    assert "_router_host" in only.message


def test_fires_outside_assert_too(audit_mod) -> None:
    """Tier 1: the whole point of this rule — a private read that never
    reaches an ``assert`` (routed through a local var, or read for a
    side effect) still fires. Rule 3 cannot see this by construction."""
    class_index = {"Foo": ({"_hot_reloader"}, "src/foo.py")}
    src = (
        "def _make_foo() -> Foo:\n"
        "    return Foo()\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    foo = _make_foo()\n"
        "    foo._hot_reloader.request_reload(source='test')\n"
    )
    (only,) = _findings_for(audit_mod, src, class_index)
    assert "_hot_reloader" in only.message


# ── Negative cases: detection MUST NOT fire ─────────────────────────────────


def test_same_attr_name_unbacked_on_this_class_does_not_fire(audit_mod) -> None:
    """Tier 1: the ``_chains`` pitfall, reproduced directly. ``Bar`` assigns
    ``self._chains`` too (per the class index, mirroring the real repo's
    SpawnTracker/RouterHostAdapter/ChainTimeoutGlue/InterAgentMessaging)
    but does NOT publish a ``chains`` @property — only some OTHER class
    (``Foo``, standing in for ``Session``) does. A global name-only index
    would misfire here; the class-scoped index must not."""
    class_index = {
        "Foo": ({"_chains"}, "src/foo.py"),  # backed: has @property chains
        "Bar": (set(), "src/bar.py"),  # self._chains assigned, no @property
    }
    src = (
        "def _make_bar() -> Bar:\n"
        "    return Bar()\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    bar = _make_bar()\n"
        "    assert bar._chains is not None\n"
    )
    findings = _findings_for(audit_mod, src, class_index)
    assert findings == []


def test_no_public_alternative_anywhere_does_not_fire(audit_mod) -> None:
    """Tier 1: an attribute name absent from the index entirely (the
    ``_audit_events`` case — #4866 dropped that half of its own title)
    never fires, regardless of local type evidence."""
    class_index = {"Foo": (set(), "src/foo.py")}
    src = (
        "def _make_foo() -> Foo:\n"
        "    return Foo()\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    foo = _make_foo()\n"
        "    assert foo._audit_events is not None\n"
    )
    findings = _findings_for(audit_mod, src, class_index)
    assert findings == []


def test_no_type_evidence_does_not_fire(audit_mod) -> None:
    """Tier 1: a bare, unannotated variable from an untyped call gives NO
    evidence — the rule must not guess from the variable's own name
    (``foo`` looking like it should be a ``Foo``)."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    src = (
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    foo = get_thing()\n"
        "    assert foo._router_host is not None\n"
    )
    findings = _findings_for(audit_mod, src, class_index)
    assert findings == []


def test_own_private_state_self_dot_x_not_flagged(audit_mod) -> None:
    """Tier 1: ``self._x`` inside the test's own helper class is the
    test's own private state, not a collaborator's — architect's own
    explicit exclusion in the #4864 thread."""
    class_index = {"Foo": ({"_router_host"}, "src/foo.py")}
    src = (
        "class _Helper:\n"
        "    def check(self):\n"
        "        assert self._router_host is None\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    _Helper().check()\n"
    )
    findings = _findings_for(audit_mod, src, class_index)
    assert findings == []


def test_direct_assignment_write_not_flagged(audit_mod) -> None:
    """Tier 1: ``client._x = value`` is a WRITE (Store context), not the
    READ this rule targets — that's #4873's territory (a real object
    mock-ified in place), a different rule. Must not double-fire here."""
    class_index = {"Foo": ({"_negotiated_version"}, "src/foo.py")}
    src = (
        "def _make_foo() -> Foo:\n"
        "    return Foo()\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    foo = _make_foo()\n"
        "    foo._negotiated_version = '2025-11-25'\n"
    )
    findings = _findings_for(audit_mod, src, class_index)
    assert findings == []


def test_dunder_excluded(audit_mod) -> None:
    """Tier 1: dunder attributes are language-level surfaces, not private
    state — same exclusion as Rule 3."""
    class_index = {"Foo": ({"__class__"}, "src/foo.py")}
    src = (
        "def _make_foo() -> Foo:\n"
        "    return Foo()\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    foo = _make_foo()\n"
        "    assert foo.__class__.__name__ == 'Foo'\n"
    )
    findings = _findings_for(audit_mod, src, class_index)
    assert findings == []


# ── Regression pin: the real Session.hot_reloader / router_host sites ───────


def test_real_corpus_class_property_index_backs_session_hot_reloader(audit_mod) -> None:
    """Tier 1: against the REAL ``src/`` tree, ``Session`` must be indexed
    with ``_hot_reloader`` backed by ``@property def hot_reloader`` — the
    exact positive control architect named in the #4864 thread (#4868 left
    4 raw ``session._hot_reloader`` sites in the very file meant to repair
    them). A regression here means the index-builder stopped seeing a
    property it used to see, silently turning the gate into a no-op."""
    index = audit_mod._build_class_property_index(REPO_ROOT / "src")
    assert "Session" in index
    backed, _file = index["Session"]
    assert "_hot_reloader" in backed
    assert "_router_host" in backed


def test_real_corpus_chains_is_not_a_global_name_match(audit_mod) -> None:
    """Tier 1: ``RouterHostAdapter`` assigns ``self._chains = chains`` (the
    exact pitfall architect named in the #4864 thread) but does not
    publish a ``chains`` @property. If the index degenerated into a
    global (does ``_chains`` exist as a property SOMEWHERE) match, a
    read on a ``RouterHostAdapter`` instance would wrongly fire the gate
    just because ``Session`` — a different class — happens to publish
    ``chains``. This proves the index stays class-scoped against the
    real source tree, not just the synthetic classes above."""
    index = audit_mod._build_class_property_index(REPO_ROOT / "src")
    assert "Session" in index
    session_backed, _file = index["Session"]
    assert "_chains" in session_backed  # sanity: Session really is backed

    assert "RouterHostAdapter" in index
    adapter_backed, _adapter_file = index["RouterHostAdapter"]
    assert "_chains" not in adapter_backed
