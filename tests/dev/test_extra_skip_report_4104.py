"""Tier 1: Contract — reyn.dev.testing.extra_skip_report, the #4104
structural gate.

Same style as tests/dev/test_network_gate_3451.py: the end-to-end "does an
extra-gap skip actually get called out" behaviour is exercised via pytest's
own `pytester` fixture — a REAL, isolated inner pytest session loading ONLY
the `reyn.dev.testing.extra_skip_report` plugin against a tiny inner test
file. This is the only way to get a real SKIPPED TestReport flowing through
a real pytest_sessionfinish without faking pytest's own reporting internals.
"""
from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]

INNER_CONFTEST = 'pytest_plugins = ["reyn.dev.testing.extra_skip_report"]\n'


def _run_inner(pytester: pytest.Pytester, body: str):
    pytester.makeconftest(INNER_CONFTEST)
    pytester.makepyfile(test_inner=body)
    return pytester.runpytest()


def test_importorskip_reason_is_called_out(pytester: pytest.Pytester):
    """Tier 1: a test skipped via pytest.importorskip's conventional 'not
    installed' reason phrasing triggers the loud #4104 summary line."""
    result = _run_inner(
        pytester,
        """
        import pytest

        def test_needs_missing_thing():
            pytest.importorskip(
                "reyn_test_4104_nonexistent_module", reason="reyn_test_4104_nonexistent_module not installed",
            )
        """,
    )
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines([
        "*1 test(s) skipped due to a missing optional-dependency extra*",
        "*test_inner.py::test_needs_missing_thing*not installed*",
    ])


def test_unrelated_skip_reason_is_not_called_out(pytester: pytest.Pytester):
    """Tier 1: non-vacuity — a skip for an ORDINARY reason (not an
    extra-dependency gap) does not trigger the #4104 summary. Without this,
    the gate would fire on every skip in the suite and the signal would be
    useless noise."""
    result = _run_inner(
        pytester,
        """
        import pytest

        def test_skipped_for_an_unrelated_reason():
            pytest.skip("not applicable on this platform")
        """,
    )
    result.assert_outcomes(skipped=1)
    assert "missing optional-dependency extra" not in result.stdout.str()


def test_disable_env_var_suppresses_the_summary(pytester: pytest.Pytester, monkeypatch):
    """Tier 1: REYN_DISABLE_EXTRA_SKIP_REPORT=1 turns the summary off even
    when a real extra-gap skip is present — the escape hatch every other
    dev-testing gate in this module family carries (mirrors
    network_gate's REYN_DISABLE_NETWORK_GATE)."""
    monkeypatch.setenv("REYN_DISABLE_EXTRA_SKIP_REPORT", "1")
    result = _run_inner(
        pytester,
        """
        import pytest

        def test_needs_missing_thing():
            pytest.importorskip(
                "reyn_test_4104_nonexistent_module", reason="reyn_test_4104_nonexistent_module not installed",
            )
        """,
    )
    result.assert_outcomes(skipped=1)
    assert "missing optional-dependency extra" not in result.stdout.str()


def test_no_hits_prints_nothing(pytester: pytest.Pytester):
    """Tier 1: non-vacuity for the whole mechanism — a run with zero skips
    at all produces zero #4104 output, not an empty-but-present banner."""
    result = _run_inner(
        pytester,
        """
        def test_passes():
            assert True
        """,
    )
    result.assert_outcomes(passed=1)
    assert "missing optional-dependency extra" not in result.stdout.str()
