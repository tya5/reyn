"""Tier 1: AST-based private-state detection in scripts/test_tier_audit.py.

Pins the contract change introduced 2026-05-27 (= Tier C1 dispatch). The
prior regex (``PRIVATE_ATTR_RE = r"\\.\\w+\\._\\w+"``) required a preceding
dot anchor, so bare ``assert obj._x`` — the most common private-state
assertion shape — silently passed audit. The replacement walks each
``ast.Assert``'s ``test`` expression for any ``ast.Attribute`` with a
single-underscore-prefixed name (dunder excluded), catching bare,
nested, chained, and subscript forms uniformly.

Tier 1 because the audit script is the OS-level contract surface used
by every Tier-rule PR review; false negatives here are how the
sub-discipline 6-round trap (= memory
``feedback_tier4_private_state_repeat_6_round``) reproduced across
multiple PRs without mechanical detection.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest


def _load_audit_module():
    """Import ``scripts/test_tier_audit.py`` as a module without invoking it.

    The script's filename starts with ``test_`` so pytest would collect it
    as a test module if discovered normally — explicit spec loading avoids
    the collision (and the ``TestAuditor`` / ``TestResult`` collection
    warnings the script emits at import time).

    The module is registered in ``sys.modules`` before exec because ``@dataclass``
    resolves its host module via ``sys.modules.get(cls.__module__)`` and
    raises ``AttributeError`` on the ``None`` return otherwise.
    """
    import sys
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "test_tier_audit.py"
    spec = importlib.util.spec_from_file_location("_audit_tier_audit_script", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit_mod():
    return _load_audit_module()


def _findings_for(audit_mod, source: str) -> list:
    """Return the list of private-state findings for *source* as a single test.

    Mirrors the audit script's own ``audit_file`` flow at function scope
    (= bypasses file I/O so the test can author the source inline).
    """
    auditor = audit_mod.TestAuditor(check_rules={"private-state"})
    tree = ast.parse(source)
    func = next(
        (n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        None,
    )
    assert func is not None, "test source must define exactly one test function"
    result = auditor._audit_test(
        Path("inline.py"), source, source.splitlines(), func, in_scaffold=False,
    )
    return [f for f in result.findings if f.rule == "private-state"]


# ── Positive cases: detection MUST fire ─────────────────────────────────────


def test_bare_private_attr_assertion_detected(audit_mod) -> None:
    """Tier 1: ``assert obj._x`` at the top of an expression fires.

    This is the exact form the prior regex missed (= no preceding dot
    anchor). The most common private-state shape in the wild.
    """
    src = (
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    assert tracker._daily_tokens == 100\n"
    )
    (only,) = _findings_for(audit_mod, src)
    assert "_daily_tokens" in only.message


def test_subscript_private_attr_detected(audit_mod) -> None:
    """Tier 1: ``assert mgr._timers["c1"]`` (= subscript form) fires."""
    src = (
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        '    assert mgr._timers["c1"] is None\n'
    )
    (only,) = _findings_for(audit_mod, src)
    assert "_timers" in only.message


def test_nested_private_attr_detected(audit_mod) -> None:
    """Tier 1: ``assert parent.obj._x`` (= nested form) still fires.

    Backward compatibility with the prior regex behavior; the AST walk
    catches the same nested case via the same Attribute node.
    """
    src = (
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    assert parent.obj._inner == 1\n"
    )
    (only,) = _findings_for(audit_mod, src)
    assert "_inner" in only.message


def test_chained_private_attr_detected(audit_mod) -> None:
    """Tier 1: ``assert obj._private_method().result`` chains through a
    private method — the private attribute walk still surfaces ``_private_method``.
    """
    src = (
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    assert conv._async_stack().snapshot() == []\n"
    )
    (only,) = _findings_for(audit_mod, src)
    assert "_async_stack" in only.message


def test_multiple_private_attrs_in_same_assert_single_finding(audit_mod) -> None:
    """Tier 1: one ``assert`` with several private accesses emits only ONE
    finding on the assert's line (= reviewer ergonomics; the assert is the
    smallest reportable unit, not the individual attribute).
    """
    src = (
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    assert obj._x == obj._y\n"
    )
    (only,) = _findings_for(audit_mod, src)
    # The first private attr in walk order anchors the finding's message.
    assert "_" in only.message


# ── Negative cases: detection MUST NOT fire ─────────────────────────────────


def test_dunder_excluded(audit_mod) -> None:
    """Tier 1: dunder attributes (``__class__`` / ``__name__`` / ``__init__``)
    are language-level surfaces, not private state. Must NOT fire.
    """
    src = (
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    assert obj.__class__.__name__ == 'Foo'\n"
        "    assert callable(cls.__init__)\n"
    )
    findings = _findings_for(audit_mod, src)
    assert findings == []


def test_public_attr_not_flagged(audit_mod) -> None:
    """Tier 1: public attribute access is not private state. Sanity check."""
    src = (
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    assert obj.public_field == 42\n"
        "    assert other.value is True\n"
    )
    findings = _findings_for(audit_mod, src)
    assert findings == []


def test_private_in_module_import_not_flagged(audit_mod) -> None:
    """Tier 1: ``from mod._private import X`` at module scope is an import,
    not an assert. The AST walker only inspects ``ast.Assert`` value trees,
    so import statements never surface. Sanity test the scope guard.
    """
    src = (
        "from somepkg._internal import helper\n"
        "\n"
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    assert helper(1) == 1\n"
    )
    findings = _findings_for(audit_mod, src)
    assert findings == []


def test_private_in_docstring_not_flagged(audit_mod) -> None:
    """Tier 1: text mentioning ``._private`` inside a docstring or comment
    is not an ``ast.Attribute`` access. Sanity check that text references
    don't trigger.
    """
    src = (
        "def test_thing():\n"
        '    """Tier 2: behavior pin; do not assert on obj._internal."""\n'
        "    # Same here: obj._x is mentioned in a comment.\n"
        "    assert obj.public == 1\n"
    )
    findings = _findings_for(audit_mod, src)
    assert findings == []


def test_private_in_assert_message_not_flagged(audit_mod) -> None:
    """Tier 1: an assert's failure message (= the second arg) may contain
    text referencing private attrs; that's a debug hint, not a state probe.
    Detection scopes to ``stmt.test`` only, so the msg expression is out
    of scope by design.
    """
    src = (
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        '    assert obj.public, f"expected public; saw {obj._debug_state!r}"\n'
    )
    findings = _findings_for(audit_mod, src)
    assert findings == []


# ── Regression: regex-anchored detection still works ────────────────────────


def test_legacy_regex_anchored_form_still_detected(audit_mod) -> None:
    """Tier 1: the original regex matched ``parent.obj._x`` (= preceding dot
    anchor). The AST replacement must still cover that shape so the rule
    has no regression versus the legacy implementation.
    """
    src = (
        "def test_thing():\n"
        '    """Tier 2: example."""\n'
        "    assert root.session._intervention_overrides.get('c') is bus\n"
    )
    (only,) = _findings_for(audit_mod, src)
    # The earliest ``_intervention_overrides`` is the first private hit.
    assert "_intervention_overrides" in only.message
