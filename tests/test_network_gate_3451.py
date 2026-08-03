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


# ── #3662: @replay marker presence must NOT itself authorize a real reach ───
#
# The gate used to treat `node.get_closest_marker("replay") is not None` as
# authorization on its own (the #3451 bootstrap rationale: record mode calls
# back into what it captured as "the original litellm.<attr>", which with
# this gate installed IS this wrapper, so letting record-mode through is what
# stops recording from blocking itself). That rationale only justifies the
# EXPLICIT, operator-typed `REYN_LLM_RECORD=1` signal — a test merely
# CARRYING the marker says nothing about operator intent (its fixture could
# be missing/corrupted by accident, #3660/#3662). These two tests assert
# behaviourally (does a real connection get attempted — a refused loopback
# port surfaces its OWN connection-refused error distinct from
# UnpinnedNetworkReach) rather than on message text (Tier 4).


def test_replay_marker_alone_no_longer_authorizes_a_real_reach(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Tier 1: #3662 — a test carrying @pytest.mark.replay, with
    REYN_LLM_RECORD unset, must be rejected by the gate the same as any
    other unpinned test. Before the fix this reached a real loopback
    connection attempt (proven by the refused-port error surfacing instead
    of UnpinnedNetworkReach); after the fix UnpinnedNetworkReach fires
    BEFORE any socket is touched."""
    monkeypatch.delenv("REYN_LLM_RECORD", raising=False)
    result = _run_inner(
        pytester,
        monkeypatch,
        """
        import pytest
        from reyn.dev.testing.network_gate import UnpinnedNetworkReach

        @pytest.mark.replay("fixtures/llm/nonexistent/doesnt_matter.jsonl")
        async def test_reach():
            import litellm
            with pytest.raises(UnpinnedNetworkReach):
                await litellm.acompletion(
                    model="openai/gpt-4o-mini",
                    messages=[{"role": "user", "content": "hi"}],
                    api_base="http://127.0.0.1:9",
                    api_key="dummy",
                    num_retries=0,
                )
        """,
        tmp_path / "events.jsonl",
    )
    result.assert_outcomes(passed=1)


def test_record_env_var_still_authorizes_the_bootstrap_reach(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Tier 1: #3662 — the ONE real constraint #3451 named (record mode calls
    back into its own captured "original", which with this gate installed IS
    the gate wrapper) must still work: REYN_LLM_RECORD=1 lets a call through
    regardless of any marker. Proven by the refused-port error surfacing
    (real reach happened) rather than UnpinnedNetworkReach. If this test
    ever goes red, fixture generation itself is broken — no "probably still
    works"."""
    monkeypatch.setenv("REYN_LLM_RECORD", "1")
    result = _run_inner(
        pytester,
        monkeypatch,
        """
        import pytest
        from reyn.dev.testing.network_gate import UnpinnedNetworkReach

        async def test_reach():
            import litellm
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
