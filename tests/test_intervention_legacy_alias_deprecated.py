"""Tier 2: legacy ``InterventionBus`` alias is no longer used in OS code
(issue #254 Phase 5 cleanup).

Pins the post-Phase-5 invariants:

  - ``InterventionBus`` is preserved as a module-level alias of
    ``RequestBus`` so external callers / restored snapshots that
    imported the legacy name keep working.
  - The alias is absent from ``__all__`` so ``from reyn.user_intervention
    import *`` no longer pulls it (= signals deprecation to discovery
    tools).
  - **No in-tree production module imports the legacy name** for type
    hints / parameter annotations. The grep-pin walks the AST of every
    module under ``src/reyn/`` and reports any leftover reference so
    drift cannot land silently.

The OS-layer migration completeness check is what lead-coder's Phase 5
scope guard required (= "全 OS caller が新 ``RequestBus`` import を
grep + Tier 2 で verify"). Phase 5 ships that verify.

Channel-implementation modules (= ``runtime/session.py`` /
``web/a2a_intervention.py``) intentionally retain references in
**docstrings** that explain the backwards-compat history. The AST
walker ignores string literals, so docstring mentions don't trip the
check — only actual ``Name`` / ``Attribute`` / ``Import`` / ``ImportFrom``
nodes count.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import reyn

# ── 1. Module-level alias preserved for backwards-compat ───────────────


def test_intervention_bus_alias_still_importable() -> None:
    """Tier 2: the legacy ``InterventionBus`` name is still importable
    via ``from reyn.user_intervention import InterventionBus`` so
    third-party code that pinned the old name keeps working.
    """
    from reyn.user_intervention import InterventionBus, RequestBus

    # Alias identity: legacy name IS the new Protocol.
    assert InterventionBus is RequestBus


def test_intervention_bus_alias_absent_from_all() -> None:
    """Tier 2: the legacy name is excluded from ``__all__`` so
    star-imports + IDE discovery surface only the canonical
    ``RequestBus`` name.
    """
    import reyn.user_intervention as mod

    assert "InterventionBus" not in mod.__all__, (
        "InterventionBus must be excluded from __all__ post-Phase-5"
    )
    assert "RequestBus" in mod.__all__
    assert "UserChannel" in mod.__all__


# ── 2. AST walker — finds legacy-name references in production code ────


def _names_used_in_source(src: str) -> set[str]:
    """Return every Name / Attribute / imported identifier referenced
    in *src*, EXCLUDING string literals and docstrings.
    """
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    names.add(alias.asname)
    return names


def _reyn_src_root() -> Path:
    """Return the path to ``src/reyn/`` (the production code tree)."""
    return Path(inspect.getfile(reyn)).resolve().parent


# Modules where the legacy name is intentionally kept (= the alias
# definition site + the migration audit test itself). These are
# allow-listed because the alias is consciously preserved as
# backwards-compat — not a drift to clean up.
_LEGACY_NAME_ALLOWLIST: set[str] = {
    # The alias-definition site itself: it assigns InterventionBus = RequestBus.
    "src/reyn/user_intervention.py",
}


def test_no_os_caller_uses_legacy_intervention_bus_name() -> None:
    """Tier 2: walk every .py file under ``src/reyn/`` and assert that
    no module references ``InterventionBus`` as a Name / Attribute /
    Import identifier — except the alias-definition site itself.

    Docstring mentions of "InterventionBus" (= explanations of the
    backwards-compat history in ``runtime/session.py`` /
    ``web/a2a_intervention.py``) are intentionally allowed because they
    live in string literals which the AST walker ignores. Only actual
    code-level references count.

    issue #254 Phase 5: scope guard pin per lead-coder owner-decision
    waiver — "全 OS caller が新 ``RequestBus`` import を grep + Tier
    2 で verify".
    """
    reyn_root = _reyn_src_root()
    project_root = reyn_root.parent.parent  # .../src → project root
    leaks: list[str] = []

    for py_file in reyn_root.rglob("*.py"):
        rel = py_file.relative_to(project_root).as_posix()
        if rel in _LEGACY_NAME_ALLOWLIST:
            continue
        src = py_file.read_text(encoding="utf-8")
        if "InterventionBus" not in src:
            # Fast path — most files don't reference it at all.
            continue
        # Slower path — parse AST and check for code-level references.
        names = _names_used_in_source(src)
        if "InterventionBus" in names:
            leaks.append(rel)

    assert not leaks, (
        f"Production modules still reference the legacy "
        f"``InterventionBus`` name as code (= import / type hint / call), "
        f"not docstring. Phase 5 requires migration to ``RequestBus``. "
        f"Modules with leaks: {sorted(leaks)}"
    )


# ── 3. Documented allow-list — channel-impl docstrings remain ──────────


def test_channel_impl_docstrings_can_still_reference_legacy_name() -> None:
    """Tier 2: docstrings in ``runtime/session.py`` and
    ``web/a2a_intervention.py`` retain explanatory references to
    ``InterventionBus`` for backwards-compat history.

    The AST walker check above intentionally excludes docstring text,
    so these references don't trip the leak detector. This test pins
    that the references EXIST as expected (= verifies our allow-list
    assumption holds — if these files ever drop the explanatory
    docstrings, the AST-walker test in section 2 still passes but
    we'd lose useful migration context).
    """
    import reyn.interfaces.web.a2a_intervention as a2a
    import reyn.runtime.session as chat_session

    chat_src = inspect.getsource(chat_session)
    a2a_src = inspect.getsource(a2a)

    # Each should mention InterventionBus in some explanatory context.
    assert "InterventionBus" in chat_src, (
        "runtime/session.py docstrings should retain InterventionBus "
        "backwards-compat history references"
    )
    assert "InterventionBus" in a2a_src, (
        "web/a2a_intervention.py docstrings should retain "
        "InterventionBus backwards-compat history references"
    )

    # And the AST walker should NOT find the name as code-level in
    # either file (= references are docstring-only).
    chat_names = _names_used_in_source(chat_src)
    a2a_names = _names_used_in_source(a2a_src)
    assert "InterventionBus" not in chat_names, (
        "runtime/session.py must not reference InterventionBus as code "
        "(docstring-only is allowed)"
    )
    assert "InterventionBus" not in a2a_names, (
        "web/a2a_intervention.py must not reference InterventionBus "
        "as code (docstring-only is allowed)"
    )
