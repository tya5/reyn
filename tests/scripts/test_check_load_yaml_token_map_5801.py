"""Tier 2: #5801 — the load-yaml-token-map gate, the ``token_map=`` twin
of ``tests/scripts/test_check_load_yaml_vocabulary_5455.py``'s own
``vocabulary=`` gate.

Real filesystem fixtures throughout (a real `tmp_path` tree of `.py`
files parsed as real ASTs) — same shape as its sibling (reject variant /
accept variant / real-tree-is-clean check).

The essential witness (owner ruling, #5801 req④): writing a NEW
``_load_yaml(path, vocabulary=...)`` call with no ``token_map=`` must be
structurally caught — a NEW reyn-token-aware yaml face that forgets to
expand reyn's own token vocabulary (the real #5801 defect: profile.yaml
never called any expansion at all) turns this gate RED, not "stays
green because nobody thought to check." Python's own ``TypeError``
already catches an omitted ``token_map`` at CALL time (the parameter has
no default) — this gate is the SECOND line of defense, same reasoning
as its sibling: it would also catch a hypothetical future regression on
the SIGNATURE side (a default quietly reintroduced).
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_load_yaml_vocabulary import find_token_map_violations


def test_a_call_missing_token_map_is_flagged(tmp_path: Path) -> None:
    """Tier 2: the exact class this gate exists to close — a call to
    `_load_yaml(path, vocabulary=X)` with no `token_map=` keyword at all."""
    (tmp_path / "reader.py").write_text(
        "def f():\n"
        "    return _load_yaml(some_path, vocabulary=X)\n",
    )
    ((path, lineno),) = find_token_map_violations(tmp_path)
    assert path == tmp_path / "reader.py"
    assert lineno == 2


def test_token_map_none_explicit_is_flagged(tmp_path: Path) -> None:
    """Tier 2: an EXPLICIT `token_map=None` is a violation too, not just
    an omitted keyword — same "None collapses distinct reasons" concern
    as the vocabulary axis (a NEW face copying a neighbor's
    `token_map=None` would look plausible and mean nothing)."""
    (tmp_path / "reader.py").write_text(
        "def f():\n"
        "    return _load_yaml(some_path, vocabulary=X, token_map=None)\n",
    )
    ((path, lineno),) = find_token_map_violations(tmp_path)
    assert path == tmp_path / "reader.py"
    assert lineno == 2


def test_token_map_a_real_dict_literal_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: accept-side — a real dict literal (including an explicit
    empty `{}` — a genuine, visible "this face has no reyn-token value
    to offer" choice, distinct from an omitted/None keyword) passes."""
    (tmp_path / "reader.py").write_text(
        "def f():\n"
        "    return _load_yaml(\n"
        "        some_path, vocabulary=X,\n"
        "        token_map={\"REYN_PROJECT_DIR\": str(project_root)},\n"
        "    )\n",
    )
    assert find_token_map_violations(tmp_path) == []


def test_token_map_an_empty_dict_literal_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: accept-side — a bare `{}` is a real, explicit choice (this
    face genuinely has no reyn token to offer), not a violation the way
    `None` is."""
    (tmp_path / "reader.py").write_text(
        "def f():\n"
        "    return _load_yaml(some_path, vocabulary=X, token_map={})\n",
    )
    assert find_token_map_violations(tmp_path) == []


def test_a_differently_named_function_is_not_matched(tmp_path: Path) -> None:
    """Tier 2: noise guard — a call to a DIFFERENT function that merely
    resembles `_load_yaml` in shape is not flagged; the gate matches on
    the exact callee name."""
    (tmp_path / "reader.py").write_text(
        "def f():\n"
        "    return _load_json(some_path)\n",
    )
    assert find_token_map_violations(tmp_path) == []


def test_the_real_source_tree_is_currently_clean() -> None:
    """Tier 2: the real repo — every ACTUAL `_load_yaml(...)` call site in
    `src/` passes `token_map=` today. Runs against the true source tree,
    not a fixture — the positive control the gate exists to keep green
    (strip-verified by hand this PR: removing one real `token_map=`
    kwarg turns this exact check RED — see this PR's own description)."""
    from scripts.check_load_yaml_vocabulary import _SRC

    assert find_token_map_violations(_SRC) == []
