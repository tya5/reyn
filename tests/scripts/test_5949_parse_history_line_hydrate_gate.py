"""Tier 2: #5949 stage ①-b — the ``hydrate`` structural backstop gate
(architect's ③, issuecomment-5576144700). Real filesystem fixtures
throughout (a real ``tmp_path`` tree of ``.py`` files) — the functions
under test parse real file content via ``ast``, so faking the filesystem
would test nothing real.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_parse_history_line_hydrate_gate import (
    count_hydrate_true_in_src,
    find_calls_missing_hydrate,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_a_call_missing_hydrate_is_flagged(tmp_path: Path) -> None:
    """Tier 2: THE real-world instance — a new caller of
    ``_parse_history_line`` that forgot ``hydrate=`` entirely is exactly
    the shape :meth:`extend_history_backward_async` had before stage
    ①-b (a whole SEPARATE parse loop, never updated when stage ① added
    the flag elsewhere)."""
    src = tmp_path / "src" / "m.py"
    _write(src, "class S:\n    def f(self, line):\n        return self._parse_history_line(line)\n")
    offenders = find_calls_missing_hydrate(tmp_path, ("src",))
    assert offenders == [(src, 3)]


def test_a_call_naming_hydrate_explicitly_is_never_flagged(tmp_path: Path) -> None:
    """Tier 2: either polarity, spelled out, is not an offender — the gate
    enforces DECIDING, not a specific decision."""
    src = tmp_path / "src" / "m.py"
    _write(
        src,
        "class S:\n"
        "    def f(self, line):\n"
        "        return self._parse_history_line(line, hydrate=False)\n"
        "    def g(self, line):\n"
        "        return self._parse_history_line(line, hydrate=True)\n",
    )
    assert find_calls_missing_hydrate(tmp_path, ("src",)) == []


def test_a_multiline_call_with_hydrate_on_a_later_line_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: AST-based, not a same-line text match — the real
    :meth:`_parse_history_line` call the stage ①-b review actually wrote
    spans multiple lines (the ref/body argument on its own line, then
    ``hydrate=...`` below it) — a same-line regex would have false-
    flagged exactly this shape (measured directly against the real
    ``test_4995_threaded_transport_proxy.py``'s own multi-line call while
    building this gate)."""
    src = tmp_path / "src" / "m.py"
    _write(
        src,
        "class S:\n"
        "    def f(self, line):\n"
        "        return self._parse_history_line(\n"
        "            line,\n"
        "            hydrate=False,\n"
        "        )\n",
    )
    assert find_calls_missing_hydrate(tmp_path, ("src",)) == []


def test_a_docstring_or_comment_mention_is_never_flagged(tmp_path: Path) -> None:
    """Tier 2: a PROSE mention of the method's name — a docstring
    cross-reference or a code comment naming it — must never be counted
    as a real call site. AST-based specifically so this holds: this gate
    itself would have false-flagged its own PR's docstrings under a
    same-line text match (measured directly while building this gate —
    an earlier regex-based draft flagged 2 real docstring/comment lines
    in this exact PR)."""
    src = tmp_path / "src" / "m.py"
    _write(
        src,
        'class S:\n'
        '    def f(self, line):\n'
        '        """See self._parse_history_line(line) for the shape."""\n'
        '        # _parse_history_line(...) is called elsewhere\n'
        '        return None\n',
    )
    assert find_calls_missing_hydrate(tmp_path, ("src",)) == []


def test_the_defs_own_body_is_never_a_caller(tmp_path: Path) -> None:
    """Tier 2: ``_parse_history_line``'s own ``def`` body may reference
    itself in a docstring cross-reference or recursive call shape without
    being counted as an offending CALLER — the gate is about every OTHER
    site deciding, not the definition site."""
    src = tmp_path / "src" / "m.py"
    _write(
        src,
        "class S:\n"
        "    def _parse_history_line(self, line, *, hydrate):\n"
        "        return self._parse_history_line(line, hydrate=hydrate)\n",
    )
    assert find_calls_missing_hydrate(tmp_path, ("src",)) == []


def test_exactly_one_hydrate_true_in_src_is_the_expected_population(tmp_path: Path) -> None:
    """Tier 2: the exactly-one constraint's happy path."""
    src = tmp_path / "src" / "m.py"
    _write(
        src,
        "class S:\n"
        "    def f(self, line):\n"
        "        return self._parse_history_line(line, hydrate=True)\n"
        "    def g(self, line):\n"
        "        return self._parse_history_line(line, hydrate=False)\n",
    )
    count, hits = count_hydrate_true_in_src(tmp_path)
    assert count == 1
    assert hits == [(src, 3)]


def test_two_hydrate_true_call_sites_in_src_is_flagged(tmp_path: Path) -> None:
    """Tier 2: THE real-world instance the exactly-one check exists to
    catch — a SECOND caller landing on the expensive path without anyone
    having decided it should (architect: "本体が実際に要るのは wire を組む
    地点だけ")."""
    a = tmp_path / "src" / "a.py"
    b = tmp_path / "src" / "b.py"
    _write(a, "class S:\n    def f(self, line):\n        return self._parse_history_line(line, hydrate=True)\n")
    _write(b, "class T:\n    def g(self, line):\n        return self._parse_history_line(line, hydrate=True)\n")
    count, hits = count_hydrate_true_in_src(tmp_path)
    assert count == 2
    assert sorted(hits) == sorted([(a, 3), (b, 3)])


def test_hydrate_true_in_a_test_file_does_not_count_toward_the_src_population(
    tmp_path: Path,
) -> None:
    """Tier 2: the exactly-one constraint is a PRODUCTION cost claim
    (``src/`` only) — a test file directly unit-testing the eager path
    with ``hydrate=True`` is legitimate test authorship, not a new
    production caller, and must not move the count."""
    src = tmp_path / "src" / "m.py"
    tests = tmp_path / "tests" / "test_m.py"
    _write(src, "class S:\n    def f(self, line):\n        return self._parse_history_line(line, hydrate=True)\n")
    _write(tests, "def test_x(session, line):\n    session._parse_history_line(line, hydrate=True)\n")
    count, hits = count_hydrate_true_in_src(tmp_path)
    assert count == 1
    assert hits == [(src, 3)]


def test_the_real_repo_tree_is_currently_clean() -> None:
    """Tier 2: the gate's own starting population — verified against the
    real, current tree (not assumed), matching
    ``check_bare_tests_import_reference.py``'s own "run it before shipping
    it" discipline. #5949 stage ①-b's own migration is what FIRST brought
    this population to (0 missing, exactly 1 hydrate=True); this asserts
    it stayed there."""
    from scripts.check_parse_history_line_hydrate_gate import _ROOT

    missing = find_calls_missing_hydrate(_ROOT)
    assert missing == [], (
        f"real caller(s) missing hydrate=: {missing} — every "
        "_parse_history_line(...) call must decide explicitly"
    )
    count, hits = count_hydrate_true_in_src(_ROOT)
    assert count == 1, (
        f"expected exactly 1 hydrate=True call site under src/, found "
        f"{count}: {hits}"
    )
