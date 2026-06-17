"""Tier 2: OS invariant — config-derived Agent construction goes via from_config (#997 dir2 PR-B).

Construction-time omit-prevention (condition d of #997 dir2): a config-derived
caller must NOT raw-construct ``SkillRuntime(...)`` — it must go through
``SkillRuntime.from_config``, which derives the permission/runtime bundle
(permission_resolver / mcp_servers / python_allowed_modules / prompt_cache_enabled
/ sandbox_config / resolver) so the FP-0008 / #1133 wiring gap cannot recur (a
caller forgetting permission_resolver → shell filtered out of the op catalog →
the LLM hallucinates a fake schema). ``SkillRuntime.from_config`` is an attribute call,
so it is not a bare construction and not flagged.

This is the third layer of the #1133-class defense: dir3 ``phase_op_catalog_gap``
runtime detection (#1152) + PR-A factory construct-test (#1155) + this pin.

Exemptions are the two parent-propagation paths that spawn an Agent from an
already-wired parent context (NOT a fresh ReynConfig), so ``from_config`` does
not apply — they forward the bundle they were given:
  - ``skill/sub_skill_runner.py``: a sub-skill inherits the parent OSRuntime's
    resolver + permission_resolver.
  - ``chat/session.py``: Session's agent-spawn forwards the session's own
    ``self._perm`` / ``self._resolver`` / ``self._mcp_servers``, all wired at
    session construction.
"""
from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "reyn"

# Parent-propagation exemptions (paths relative to src/reyn). These construct an
# Agent from an already-wired parent context, not a fresh config — from_config
# (config → bundle) does not apply to them.
_EXEMPT = {
    "skill/sub_skill_runner.py",
    "chat/session.py",
}


def _bare_agent_constructions() -> list[tuple[str, int]]:
    """Every bare ``SkillRuntime(...)`` call (func is the Name ``Agent``) under src/reyn.

    ``SkillRuntime.from_config(...)`` is an attribute call (func is an ``ast.Attribute``)
    and is intentionally excluded — that is the sanctioned construction path.
    """
    hits: list[tuple[str, int]] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "SkillRuntime"
            ):
                hits.append((path.relative_to(_SRC).as_posix(), node.lineno))
    return hits


def test_no_unexpected_bare_agent_construction() -> None:
    """Tier 2: bare SkillRuntime(...) appears only in the documented parent-propagation exemptions.

    Any new config-derived caller (e.g. a future docker/swebench bridge) that
    raw-constructs SkillRuntime(...) fails here — forcing it through SkillRuntime.from_config
    so it cannot omit the permission/runtime bundle (the #1133 / FP-0008 gap).
    """
    offenders = [(f, ln) for f, ln in _bare_agent_constructions() if f not in _EXEMPT]
    assert not offenders, (
        "config-derived Agent construction must go through SkillRuntime.from_config "
        "(#997 dir2 — so the permission/runtime bundle cannot be omitted, the "
        f"FP-0008 / #1133 wiring-gap class). Bare SkillRuntime(...) outside the "
        f"documented parent-propagation exemptions: {offenders}. Either migrate "
        f"to SkillRuntime.from_config, or (if it genuinely propagates an already-wired "
        f"parent bundle) add it to _EXEMPT with a rationale."
    )


def test_exemptions_are_not_stale() -> None:
    """Tier 2: each exemption still contains a bare SkillRuntime(...) (no vacuous allow-list).

    If an exempted file stops raw-constructing Agent, it must be dropped from
    _EXEMPT so the pin stays tight (an unused exemption silently widens the hole
    for a future raw construction in that file).
    """
    files_with_bare = {f for f, _ in _bare_agent_constructions()}
    stale = [f for f in _EXEMPT if f not in files_with_bare]
    assert not stale, (
        f"exemption(s) no longer contain a bare SkillRuntime(...) — remove from _EXEMPT: {stale}"
    )
