"""Tier 1: Contract — reyn.dev.testing.network_gate, the #3451 structural gate.

Two layers, two test styles:

1. The stale-marker bookkeeping (`_append_event` / `stale_allow_markers`) is
   pure file I/O — exercised directly against a real temp file (no fake).
2. The end-to-end "does an unpinned litellm reach actually fail the run"
   behaviour is exercised via pytest's own `pytester` fixture: a REAL,
   isolated inner pytest session (not a mock of pytest.Item — an actual one)
   loading ONLY the `reyn.dev.testing.network_gate` plugin against a tiny
   inner test file. This is the only way to get a real `pytest.Item` carrying
   real markers without either faking one or mutating the outer test's own
   node.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.dev.testing import network_gate

pytest_plugins = ["pytester"]

# The inner pytester session loads ONLY the gate plugin — not this repo's
# whole tests/conftest.py, which has other fixtures (env-identity loading,
# secrets-path redirection, ...) that assume this repo's real directory
# layout and would break inside pytester's throwaway tmp rootdir. The gate
# module being a standalone pytest plugin (see its own module docstring) is
# what makes this possible.
INNER_CONFTEST = 'pytest_plugins = ["reyn.dev.testing.network_gate"]\n'


# ── Layer 1: stale-marker bookkeeping (real file, no pytest session needed) ──


def test_declared_without_a_matching_trigger_is_stale(tmp_path, monkeypatch):
    """Tier 1: a declared allow_real_network marker with no matching
    'triggered' event is reported by stale_allow_markers() — the #3437
    'declared ⊆ actual' direction for the exception registry."""
    monkeypatch.setenv("REYN_NETWORK_GATE_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    network_gate._append_event("declared", "tests/x.py::test_never_fires")
    assert network_gate.stale_allow_markers() == {"tests/x.py::test_never_fires"}


def test_declared_with_a_matching_trigger_is_not_stale(tmp_path, monkeypatch):
    """Tier 1: a declared marker whose test DID trigger a real call is not
    stale — the registry is doing its job, not just decorating a test."""
    monkeypatch.setenv("REYN_NETWORK_GATE_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    network_gate._append_event("declared", "tests/x.py::test_fires")
    network_gate._append_event("triggered", "tests/x.py::test_fires")
    assert network_gate.stale_allow_markers() == set()


def test_reset_events_file_clears_prior_declarations(tmp_path, monkeypatch):
    """Tier 1: reset_events_file() is what pytest_configure calls (only from
    the process that owns the whole session) so a PRIOR run's leftover
    declarations never leak into this run's staleness verdict."""
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("REYN_NETWORK_GATE_EVENTS_PATH", str(events_path))
    network_gate._append_event("declared", "tests/x.py::test_stale_from_last_run")
    network_gate.reset_events_file()
    assert network_gate.stale_allow_markers() == set()


# ── Layer 2: end-to-end, via a real (pytester) inner pytest session ────────


def _run_inner(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, body: str, events_path: Path,
):
    pytester.makeconftest(INNER_CONFTEST)
    pytester.makeini("[pytest]\nasyncio_mode = auto\n")
    pytester.makepyfile(test_inner=body)
    monkeypatch.setenv("REYN_NETWORK_GATE_EVENTS_PATH", str(events_path))
    return pytester.runpytest()


def test_unpinned_reach_fails_the_inner_session(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Tier 1: a test with no @replay pin and no @allow_real_network marker
    that reaches litellm.acompletion fails — loud, not silent (#3445's 38
    swallowed-exception cases turned into an attributable failure)."""
    result = _run_inner(
        pytester,
        monkeypatch,
        """
        async def test_reach():
            import litellm
            await litellm.acompletion(
                model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
            )
        """,
        tmp_path / "events.jsonl",
    )
    result.assert_outcomes(failed=1)


def test_allow_marked_reach_with_reason_is_permitted(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Tier 1: @allow_real_network(reason=...) lets the call through to the
    real litellm function (which then fails for an UNRELATED reason — a
    refused loopback port — never as UnpinnedNetworkReach)."""
    result = _run_inner(
        pytester,
        monkeypatch,
        """
        import pytest

        @pytest.mark.allow_real_network(reason="unit test: deliberate refused loopback port")
        async def test_reach():
            import litellm
            from reyn.dev.testing.network_gate import UnpinnedNetworkReach
            try:
                await litellm.acompletion(
                    model="openai/gpt-4o-mini",
                    messages=[{"role": "user", "content": "hi"}],
                    api_base="http://127.0.0.1:9",
                    api_key="dummy",
                    num_retries=0,
                )
            except UnpinnedNetworkReach:
                raise
            except Exception:
                pass  # any OTHER exception (refused connection) is expected
        """,
        tmp_path / "events.jsonl",
    )
    result.assert_outcomes(passed=1)


def test_allow_marked_without_reason_is_rejected(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Tier 1: @allow_real_network with no reason= is treated the same as no
    marker at all — an unexplained exception is as bad as an undeclared one."""
    result = _run_inner(
        pytester,
        monkeypatch,
        """
        import pytest

        @pytest.mark.allow_real_network()
        async def test_reach():
            import litellm
            await litellm.acompletion(
                model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
            )
        """,
        tmp_path / "events.jsonl",
    )
    result.assert_outcomes(failed=1)
