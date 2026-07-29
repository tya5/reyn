"""Tier 2: OS invariant — #3408 ``self._chat_events`` single-assignment AST guard.

#2856's accident was NOT "a stale value from a re-assignment" — it was a
NAME reference (``self.events``) that resolved to a DIFFERENT audit-event log
than the one live when the reference was written, silently disabling
``search_actions`` for every operator (FP-0043 C.3/C.4). #3408 measured that
``self._chat_events`` is written exactly ONCE repo-wide (``git grep
'_chat_events =' -- src`` -> ``session.py:1279`` only, inside
``Session.__init__``) and used that fact to replace a deferred NAME lookup
(``Session._build_retrieval_bundle``'s ``_on_hot_list_changed`` closure
resolving ``self._chat_events`` at call time) with an IDENTITY binding (the
``chat_events`` object passed as a builder arg, captured once).

Identity binding is only safe *because* the write is single — if a future
restore/attach path re-assigns ``self._chat_events`` after construction, an
identity-bound closure built from the FIRST value would keep pointing at a
stale/replaced EventLog while every NAME-based reader would see the new one.
This guard makes that precondition a CHECKED invariant instead of a comment:
the day a second ``self._chat_events = ...`` assignment site appears anywhere
in ``src/reyn``, this test goes RED, naming the new file:line and saying
"route the identity-bound closures back through a live NAME lookup instead."

AST-based (not regex): a regex on ``_chat_events =`` would also match dict
keys, docstring prose, and comments — none of which are assignments. Only an
``ast.Assign``/``ast.AugAssign`` target of ``self._chat_events`` counts.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("repo root not found from " + str(here))


def _is_self_chat_events_target(node: ast.expr) -> bool:
    """True for a ``self._chat_events`` attribute-assignment TARGET."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "_chat_events"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _find_assignment_sites(py: Path) -> list[int]:
    tree = ast.parse(py.read_text(encoding="utf-8"))
    sites: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _is_self_chat_events_target(target):
                    sites.append(node.lineno)
        elif isinstance(node, ast.AugAssign) and _is_self_chat_events_target(node.target):
            sites.append(node.lineno)
    return sites


def test_self_chat_events_assigned_exactly_once_src_wide() -> None:
    """Tier 2: ``self._chat_events = ...`` has exactly ONE assignment site in
    ``src/reyn`` — the precondition #3408's identity-bound
    ``_on_hot_list_changed`` closure (``Session._build_retrieval_bundle``)
    relies on. A second site means a restore/attach path can now re-target
    the log after construction, which an identity-bound closure would miss —
    this must go RED before that lands, not be discovered later as a silent
    stale-sink bug."""
    root = _repo_root()
    src = root / "src" / "reyn"

    offenders: dict[str, list[int]] = {}
    for py in src.rglob("*.py"):
        sites = _find_assignment_sites(py)
        if sites:
            offenders[str(py.relative_to(root))] = sites

    total = sum(len(v) for v in offenders.values())
    assert total == 1, (
        "self._chat_events assignment count changed from the single-write "
        "invariant #3408's identity binding depends on (expected exactly 1, "
        f"found {total}): {offenders}. If this is a deliberate new "
        "restore/attach re-assignment path, Session._build_retrieval_bundle's "
        "_on_hot_list_changed closure must go back to a live NAME lookup "
        "(self._chat_events, resolved at call time) instead of the identity-"
        "bound builder arg — see that method's docstring."
    )
    [(only_file, _only_lines)] = offenders.items()
    assert only_file == "src/reyn/runtime/session.py", (
        "the sole self._chat_events assignment moved out of "
        f"runtime/session.py to {only_file} — Session.__init__ is the "
        "expected owner (Family 1 / _build_audit_event_bundle)"
    )
