"""Tier 2: main-red 2026-09-10 (CI finding) — `tests/conftest.py`'s
`_isolate_root_logging_handlers` autouse fixture actually detaches a
stray handler a test leaves on the root logger, and restores root's own
level.

The fixture ALSO calls `logging.captureWarnings(False)` in its own
teardown, matching every individual `_setup_interactive_logging` caller's
own manual cleanup — deliberately NOT given a dedicated test here: tried
by hand first (the same `pytester` shape below), and pytest's OWN
built-in warnings plugin already wraps each test's setup+call+teardown in
its own `warnings.catch_warnings()`, which independently resets
`warnings.showwarning` regardless of this fixture — so a test built this
way stayed green with that specific line commented out (a Q4 violation:
green with nothing of THIS fixture's own to bite on). The line stays (it
is still correct production-cleanup-parity, and free), the false witness
does not.

Owner-visible background: `main` went red with 2 unrelated-looking
failures (`test_stderr_summary_bypasses_a_rebound_sys_stderr` in
`test_5939_pr1_cache_release_and_forensics.py`, and
`test_with_no_target_ever_attached_the_exit_fallback_reaches_stderr` in
`test_5989_early_log_buffer.py`) after #6045 (`chat._setup_interactive_
logging` now attaches a bare `logging.StreamHandler()` — binding WHATEVER
`sys.stderr` is live at that exact call — whenever `is_interactive=False`)
landed. Both target tests' OWN claims re-verified as still true in
isolation (their properties were never false): the actual mechanism is a
stray `StreamHandler` left attached to root by SOME test elsewhere,
surfacing as a "Logging error" traceback (a `ValueError: I/O operation on
closed file` inside a completely unrelated test's own `emit()`, since
pytest's own per-test capture stream that handler was bound to gets
closed at THAT test's teardown) or a live write into a LATER test's own
monkeypatched `sys.stderr`. Every individual caller of
`_setup_interactive_logging` in `tests/` already saves+restores
`root.handlers` itself — this fixture is the SAME process-level net
`_isolate_stall_trace_file_handler_registration` (this file's own
sibling autouse fixture) already provides for a different process-global
(`stall_trace`'s registered path): a process-global a test can mutate is
this file's own responsibility to isolate, not every individual caller's.

Real, isolated inner pytest sessions throughout (`pytester`'s own
subprocess seam — the SAME technique
`test_isolate_litellm_process_globals_5918.py`'s own witness uses for a
different conftest fixture), never a mock of pytest's own fixture
machinery: the actual `tests/conftest.py` fixture, actual `logging`
module."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

#: Same shape as test_isolate_litellm_process_globals_5918.py's own
#: `_INNER_CONFTEST`: puts the repo root + src on sys.path then imports
#: the REAL autouse fixture from the REAL tests/conftest.py — pytest
#: activates any @pytest.fixture-decorated callable present in a conftest
#: module's namespace, imported or not.
_INNER_CONFTEST = """
import sys
sys.path.insert(0, {repo_root!r})
sys.path.insert(0, {src_root!r})
from tests.conftest import _isolate_root_logging_handlers  # noqa: F401
"""

#: test_a leaves a stray StreamHandler attached to root, bound to a
#: throwaway StringIO — the EXACT shape #6045's `is_interactive=False`
#: branch creates (a bare `logging.StreamHandler()`, no ownership
#: tracking of its own) — WITHOUT any cleanup of its own, simulating a
#: caller that never restores `root.handlers`. test_b, a SEPARATE test in
#: the SAME inner session, checks root is clean — so the fixture's own
#: per-test before/after hooks are the only thing that could have cleaned
#: up between them, nothing else runs.
_INNER_TEST_POLLUTION = """
import io
import logging

_STRAY_STREAM = []
_LEVEL_BEFORE_POLLUTION = []

def test_a_leaves_a_stray_handler_and_a_changed_level_without_cleaning_up():
    root = logging.getLogger()
    _LEVEL_BEFORE_POLLUTION.append(root.level)
    stream = io.StringIO()
    _STRAY_STREAM.append(stream)
    root.addHandler(logging.StreamHandler(stream))
    root.setLevel(logging.DEBUG)
    assert any(
        isinstance(h, logging.StreamHandler) and h.stream is stream for h in root.handlers
    )  # sanity

def test_b_starts_with_no_stray_handler_original_level_and_capture_warnings_off():
    assert _STRAY_STREAM, "setup: test_a must run first, in this same process"
    root = logging.getLogger()
    assert not any(
        isinstance(h, logging.StreamHandler) and h.stream is _STRAY_STREAM[-1]
        for h in root.handlers
    ), (
        "test_a's own stray StreamHandler leaked into test_b -- the isolation "
        "fixture must DETACH any handler a test adds, not just leave root's "
        "handler list for a later caller to inherit (main-red's own shape: a "
        "LATER, unrelated test's teardown closes the stream this handler still "
        "references, so ITS write raises there instead)"
    )
    assert root.level == _LEVEL_BEFORE_POLLUTION[-1], (
        f"root.level leaked from test_a ({logging.DEBUG}) into test_b -- the "
        f"isolation fixture must restore it to what it was BEFORE test_a ran "
        f"({_LEVEL_BEFORE_POLLUTION[-1]!r})"
    )
"""


def test_isolation_detaches_a_stray_handler_and_restores_level(
    pytester: pytest.Pytester,
) -> None:
    """Tier 2: main-red acceptance ① (a stray handler does not survive
    into the next test) + ② (root's own level is restored) — driven
    together since both are the SAME fixture's before/after halves, in
    ONE real isolated inner pytest session so ordering is guaranteed
    (pytester's own subprocess, not `-n auto` — the outer suite's own
    parallelism is exactly what this fixture exists to make irrelevant,
    so the WITNESS must not depend on it either).

    Strip (①): comment out the `for handler in root.handlers: ... handler
    .close()` / `root.handlers[:] = saved_handlers` lines in the fixture
    — `test_b` goes red (the stray handler is still attached).
    Strip (②): comment out `root.setLevel(saved_level)` — `test_b` goes
    red (`root.level` stays `DEBUG`)."""
    import reyn

    repo_root = Path(reyn.__file__).resolve().parents[2]
    src_root = str(repo_root / "src")

    pytester.makeconftest(_INNER_CONFTEST.format(repo_root=str(repo_root), src_root=src_root))
    pytester.makepyfile(test_inner=_INNER_TEST_POLLUTION)

    result = pytester.runpytest_subprocess("test_inner.py")
    result.assert_outcomes(passed=2)
