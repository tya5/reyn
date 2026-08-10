"""Tier 2: OS invariant — #2708 P1 present-sink single-writer AST guard.

``OutboxPresentationRenderer(...)`` (the outbox-backed present sink, whose output
reaches the user ONLY if a per-surface drain consumes it) may be instantiated in
``src/reyn`` ONLY inside ``OutboxPresentationConsumer.sink`` (in
``runtime/presentation_consumer.py``). Any other construction site is an *orphan*
sink — a renderer with no consumer draining its outbox — the exact #2688/#2707 bug
class. This guard makes that orphan *impossible by construction* (the #1190
``litellm.acompletion`` / #2683 single-writer model): a new
``OutboxPresentationRenderer(...)`` anywhere else in ``src/reyn`` fails here at PR
time, naming file:line. Tests are exempt (the guard scopes to ``src/reyn``), so
fixtures may construct the renderer directly.
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


def _is_outbox_renderer_construction(node: ast.AST) -> bool:
    """True for an ``OutboxPresentationRenderer(...)`` CALL (construction).

    Only a call constructs the sink; a bare ``Name`` reference / import is not a
    construction (excluded), matching the #1190 guard's call-vs-reference split.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "OutboxPresentationRenderer"
    )


def _sink_method_span(consumer_py: Path) -> tuple[int, int]:
    """Return the (start, end) line span of ``OutboxPresentationConsumer.sink``."""
    tree = ast.parse(consumer_py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OutboxPresentationConsumer":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    item.name == "sink"
                ):
                    return item.lineno, (item.end_lineno or item.lineno)
    raise AssertionError(
        "OutboxPresentationConsumer.sink not found in presentation_consumer.py — "
        "the present-sink chokepoint moved?"
    )


def test_outbox_present_renderer_constructed_only_in_consumer_sink() -> None:
    """Tier 2: the outbox present sink's sole construction site is
    OutboxPresentationConsumer.sink (orphan sinks structurally impossible)."""
    root = _repo_root()
    src = root / "src" / "reyn"
    consumer_py = (src / "runtime" / "presentation_consumer.py").resolve()
    start, end = _sink_method_span(consumer_py)

    offenders: list[str] = []
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not _is_outbox_renderer_construction(node):
                continue
            inside = py.resolve() == consumer_py and start <= node.lineno <= end
            if not inside:
                offenders.append(f"{py.relative_to(root)}:{node.lineno}")

    assert not offenders, (
        "OutboxPresentationRenderer(...) constructed outside "
        "OutboxPresentationConsumer.sink — an orphan present sink (renderer with no "
        "consumer draining its outbox = the #2688/#2707 silent-invisible-present bug). "
        "Obtain the sink via a PresentationConsumer.sink(session) instead. "
        f"Offending sites: {offenders}"
    )


def test_sink_chokepoint_actually_constructs_it() -> None:
    """Tier 2: positive guard — OutboxPresentationConsumer.sink DOES construct the
    renderer (the chokepoint exists and is used, not merely asserted-empty)."""
    root = _repo_root()
    consumer_py = (root / "src" / "reyn" / "runtime" / "presentation_consumer.py").resolve()
    start, end = _sink_method_span(consumer_py)
    tree = ast.parse(consumer_py.read_text(encoding="utf-8"))
    constructions = [
        node.lineno
        for node in ast.walk(tree)
        if _is_outbox_renderer_construction(node) and start <= node.lineno <= end
    ]
    assert constructions, (
        "OutboxPresentationConsumer.sink must construct OutboxPresentationRenderer "
        "(the single allowed present-sink chokepoint) — none found"
    )
