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

architect BLOCKING finding on this PR's first revision: a bare
``vocabulary=None`` collapses THREE distinct, real reasons ("checked by
config validate downstream", "checked at its own hot-reload load point",
"checked by the immediate caller") into one value nothing can tell
apart from "nobody decided" — a future contributor would copy a
neighboring ``vocabulary=None`` for a genuinely NEW file without
checking whether it is actually validated anywhere, reopening the
#4501/#4515 hole this issue closes while this gate stayed green (a bare
presence check does not see WHAT was passed). Fixed: ``vocabulary``
accepts only a callable or a named ``_CheckedElsewhere`` member; ``None``
is now flagged exactly like an omitted keyword — see the reject/accept
pair below.
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


def test_vocabulary_none_explicit_is_flagged(tmp_path: Path) -> None:
    """Tier 2: architect's own BLOCKING finding, re-verified here as a
    reject case — an EXPLICIT `vocabulary=None` is now a violation too,
    not just an omitted keyword. `None` collapses 3 distinct reasons
    into one value nothing can distinguish from "nobody decided"."""
    (tmp_path / "reader.py").write_text(
        "def f():\n"
        "    return _load_yaml(some_path, vocabulary=None)\n",
    )
    ((path, lineno),) = find_violations(tmp_path)
    assert path == tmp_path / "reader.py"
    assert lineno == 2


def test_vocabulary_a_named_checked_elsewhere_member_is_not_flagged(
    tmp_path: Path,
) -> None:
    """Tier 2: accept-side — a real, named _CheckedElsewhere member (the
    replacement for a bare None) passes."""
    (tmp_path / "reader.py").write_text(
        "def f():\n"
        "    return _load_yaml(\n"
        "        some_path, vocabulary=_CheckedElsewhere.CHECKED_BY_CALLER,\n"
        "    )\n",
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
