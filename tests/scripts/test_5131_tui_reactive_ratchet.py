"""Tier 2: #5131 gate B — scripts/check_tui_reactive_ratchet.py.

Placed in tests/scripts/ (not flat) — mirrors test_flat_tests_ratchet_3879.py's
own placement rationale. Real files on a real tmp_path throughout (the
functions under test are thin AST walks over the filesystem) — no mocks.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_tui_reactive_ratchet import (
    measured_imperative_push_count,
    measured_reactive_count,
)

_REACTIVE_FILE = '''
from textual.reactive import reactive


class Widget:
    _agent_name = reactive("")

    def watch__agent_name(self, old_value, new_value):
        pass
'''

_NO_REACTIVE_FILE = '''
class Widget:
    def __init__(self):
        self._agent_name = ""
'''

_APP_PUSH_FILE = '''
class App:
    def refresh(self):
        self.query_one("#status", Static).update("x")
        self.query_one("#drawer", ContentSwitcher).focus()
'''

_APP_NO_PUSH_FILE = '''
class App:
    def refresh(self):
        widget = self.query_one("#status", Static)
        widget.update("x")
'''


def _write(tests_dir: Path, name: str, content: str) -> Path:
    p = tests_dir / name
    p.write_text(content, encoding="utf-8")
    return p


def test_reactive_count_sums_declarations_and_watch_methods_across_files(
    tmp_path: Path,
) -> None:
    """Tier 2: one reactive() assignment + one watch_ method in ONE file ->
    count 2; a sibling file with neither contributes 0 — the count sums
    across every .py file in the package, not just one."""
    _write(tmp_path, "widget.py", _REACTIVE_FILE)
    _write(tmp_path, "other.py", _NO_REACTIVE_FILE)

    assert measured_reactive_count(tmp_path) == 2


def test_reactive_count_is_zero_with_no_reactive_usage(tmp_path: Path) -> None:
    """Tier 2: a package with no reactive()/watch_ at all counts 0 — the
    floor a from-scratch package starts at."""
    _write(tmp_path, "other.py", _NO_REACTIVE_FILE)

    assert measured_reactive_count(tmp_path) == 0


def test_imperative_push_count_counts_every_query_one_method_call(
    tmp_path: Path,
) -> None:
    """Tier 2: BOTH self.query_one(...).update(...) (a state push) AND
    self.query_one(...).focus() (a pure action) count — the gate is
    deliberately UNDER-selective (see the script's own docstring for why:
    distinguishing the two is the semantic question Gate A already
    declined to answer)."""
    app_py = _write(tmp_path, "app.py", _APP_PUSH_FILE)

    assert measured_imperative_push_count(app_py) == 2


def test_imperative_push_count_is_zero_when_widgets_are_looked_up_but_not_chained(
    tmp_path: Path,
) -> None:
    """Tier 2: query_one(...) assigned to a local variable, with the method
    call on a SEPARATE line, is NOT counted — the gate's own narrow shape
    (self.query_one(...).method(...) CHAINED on one expression) is a
    deliberate proxy for "reach in and push immediately," not "look up a
    widget for any reason."""
    app_py = _write(tmp_path, "app.py", _APP_NO_PUSH_FILE)

    assert measured_imperative_push_count(app_py) == 0


def test_imperative_push_count_is_zero_for_a_missing_file(tmp_path: Path) -> None:
    """Tier 2: a package with no app.py at all (shouldn't happen in
    production, but the function must not raise) measures 0, not an
    exception — mirrors measured_reactive_count's own empty-input safety."""
    assert measured_imperative_push_count(tmp_path / "does_not_exist.py") == 0
