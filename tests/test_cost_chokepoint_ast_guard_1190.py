"""Tier 2: OS invariant — #1190 stage (iii) keystone AST guard.

`litellm.acompletion` may be called ONLY inside `recorded_acompletion`
(`src/reyn/llm/llm.py`). Every other call site would bypass the cost-observability
chokepoint (no usage recording, no purpose attribution) — exactly the class of
bug #1190 closes. This guard makes that bypass *impossible by construction*, not
merely currently-absent: a new `litellm.acompletion(...)` anywhere else in
`src/reyn/` fails here at PR time (same omit-pin pattern as the #1172 resolver
guard and the #997 op-exposure guard).
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


def _is_litellm_acompletion_call(node: ast.AST) -> bool:
    """True for a ``litellm.acompletion(...)`` CALL.

    Only call sites bypass the chokepoint. An attribute *reference* /
    *assignment* (e.g. ``litellm.acompletion = self._handle`` in LLMReplay, the
    monkeypatch that the chokepoint relies on) is not a bypass — exclude it.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "acompletion"
        and isinstance(func.value, ast.Name)
        and func.value.id == "litellm"
    )


def _recorded_acompletion_span(llm_py: Path) -> tuple[int, int]:
    """Return the (start, end) line span of the ``recorded_acompletion`` def."""
    tree = ast.parse(llm_py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == "recorded_acompletion"
        ):
            return node.lineno, (node.end_lineno or node.lineno)
    raise AssertionError("recorded_acompletion not found in llm.py — chokepoint moved?")


def test_litellm_acompletion_only_inside_recorded_acompletion() -> None:
    """Tier 2: the single cost chokepoint is the only `litellm.acompletion` caller."""
    root = _repo_root()
    src = root / "src" / "reyn"
    llm_py = (src / "llm" / "llm.py").resolve()
    start, end = _recorded_acompletion_span(llm_py)

    offenders: list[str] = []
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not _is_litellm_acompletion_call(node):
                continue
            # Allowed iff inside recorded_acompletion's body in llm.py (its
            # nested `_once` helper lives within that line span).
            inside = py.resolve() == llm_py and start <= node.lineno <= end
            if not inside:
                offenders.append(f"{py.relative_to(root)}:{node.lineno}")

    assert not offenders, (
        "litellm.acompletion called outside recorded_acompletion (bypasses the "
        "#1190 cost chokepoint — no usage recording / purpose attribution). "
        "Route the call through reyn.llm.llm.recorded_acompletion(purpose=...). "
        f"Offending sites: {offenders}"
    )
