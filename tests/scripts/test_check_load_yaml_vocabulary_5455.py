"""Tier 2: #5455 ② — the load-yaml-vocabulary gate.

Real filesystem fixtures throughout (a real `tmp_path` tree of `.py`
files parsed as real ASTs) — mirrors
`tests/scripts/test_check_collect_events_settle_4966.py`'s own shape
(reject variant / accept variant / real-tree-is-clean check).

Witness ⑦ (architect's own #5455 design — "the essential witness, ①-⑥
only close THIS one issue"): writing a NEW ``_load_yaml(path)`` call with
no ``vocabulary=`` must be structurally caught. Python's own ``TypeError``
already catches this at CALL time (the parameter has no default) — this
gate is the SECOND line of defense: it would also catch a hypothetical
future regression on the SIGNATURE side (a default quietly reintroduced),
which a bare call-time TypeError could no longer do.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_load_yaml_vocabulary import find_violations


def test_a_call_missing_vocabulary_is_flagged(tmp_path: Path) -> None:
    """Tier 2: the exact class this gate exists to close — a call to
    `_load_yaml(path)` with no `vocabulary=` keyword at all."""
    (tmp_path / "reader.py").write_text(
        "def f():\n"
        "    return _load_yaml(some_path)\n",
    )
    # Unpack-enforcement idiom: exactly 1 violation must be found.
    ((path, lineno),) = find_violations(tmp_path)
    assert path == tmp_path / "reader.py"
    assert lineno == 2


def test_vocabulary_none_explicit_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: accept-side — an EXPLICIT `vocabulary=None` (a real,
    reviewable decision, per _load_yaml's own docstring) is not a
    violation; only OMITTING the keyword entirely is."""
    (tmp_path / "reader.py").write_text(
        "def f():\n"
        "    return _load_yaml(some_path, vocabulary=None)\n",
    )
    assert find_violations(tmp_path) == []


def test_vocabulary_a_real_callable_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: accept-side — a real vocabulary callable passed positively."""
    (tmp_path / "reader.py").write_text(
        "def f():\n"
        "    return _load_yaml(some_path, vocabulary=unknown_config_keys)\n",
    )
    assert find_violations(tmp_path) == []


def test_a_differently_named_function_is_not_matched(tmp_path: Path) -> None:
    """Tier 2: noise guard — a call to a DIFFERENT function that merely
    resembles `_load_yaml` in shape (same arg count, no vocabulary=) is
    not flagged; the gate matches on the exact callee name."""
    (tmp_path / "reader.py").write_text(
        "def f():\n"
        "    return _load_json(some_path)\n",
    )
    assert find_violations(tmp_path) == []


def test_the_real_source_tree_is_currently_clean() -> None:
    """Tier 2: the real repo — every ACTUAL `_load_yaml(...)` call site in
    `src/` passes `vocabulary=` today. Runs against the true source tree,
    not a fixture — this is the positive control the gate exists to keep
    green (strip-verified by hand this PR: removing one real
    `vocabulary=` kwarg turns this exact check RED)."""
    from scripts.check_load_yaml_vocabulary import _SRC

    assert find_violations(_SRC) == []
