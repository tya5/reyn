"""Tier 1: Contract — a missing @replay fixture fails loud, never records
automatically (#3662).

Mirrors ``tests/test_network_gate_3451.py``'s own pytester technique (a real,
isolated inner pytest session — not a mock of pytest.Item) rather than
loading this repo's whole ``tests/conftest.py`` (which the sibling file's
module docstring explains is unsafe inside pytester's throwaway rootdir: it
carries other fixtures assuming this repo's real directory layout). The
inner conftest below is a MINIMAL, faithful replica of the real
``_llm_replay`` fixture's mode-selection block — same shape as this repo's
own ``tests/conftest.py:_llm_replay`` after #3662 — exercised together with
the real ``reyn.dev.testing.network_gate`` plugin so the two fixes are
proven to cooperate, not just each in isolation.

Being a replica (not a re-load of the real ``tests/conftest.py``), these
tests verify the FIXED shape and guard it against regression; they do not
themselves re-demonstrate the pre-fix RED. That RED is the empirical repro
already captured with a real traceback in #3660's and #3662's issue
comments (a real fixture deleted, `ConnectionRefusedError` reaching a real
socket, silently swallowed, test green) — reproducing it here would mean
maintaining a second, parallel copy of the OLD buggy logic purely to keep
proving a historical fact. ``tests/test_network_gate_3451.py``'s new #3662
tests DO carry a live RED/GREEN pair, for the ``network_gate.py`` half of
this fix specifically.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

INNER_CONFTEST = '''
import os
from pathlib import Path

import pytest

pytest_plugins = ["reyn.dev.testing.network_gate"]


@pytest.fixture(autouse=True)
def _llm_replay(request):
    marker = request.node.get_closest_marker("replay")
    if marker is None:
        yield
        return

    fixture_path = Path(request.config.rootdir) / marker.args[0]
    force_record = os.environ.get("REYN_LLM_RECORD") == "1"
    mode = "record" if force_record else "replay"

    if not force_record and not fixture_path.exists():
        pytest.fail(
            f"Replay fixture not found: {fixture_path}\\n"
            f"Re-run with: REYN_LLM_RECORD=1 python -m pytest {request.node.nodeid}\\n"
            "(this makes a real LLM call and writes the fixture)."
        )

    from reyn.dev.testing.replay import LLMReplay

    replay = LLMReplay(fixture_path, mode=mode)
    replay.install()
    try:
        yield replay
    finally:
        replay.restore()
        if mode == "record":
            replay.flush()
'''


def _run_inner(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, body: str, events_path: Path,
):
    pytester.makeconftest(INNER_CONFTEST)
    pytester.makeini("[pytest]\nasyncio_mode = auto\n")
    pytester.makepyfile(test_inner=body)
    monkeypatch.setenv("REYN_NETWORK_GATE_EVENTS_PATH", str(events_path))
    return pytester.runpytest()


def test_missing_fixture_fails_before_any_network_reach(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Tier 1: #3662 — a @replay-marked test whose fixture file does not
    exist, with REYN_LLM_RECORD unset, fails at fixture setup — the test
    BODY (and any litellm call it would have made) never runs at all. Proven
    behaviourally: the body would raise AssertionError('body ran') if it
    executed, and it must not."""
    monkeypatch.delenv("REYN_LLM_RECORD", raising=False)
    result = _run_inner(
        pytester,
        monkeypatch,
        """
        import pytest

        @pytest.mark.replay("fixtures/llm/nonexistent/missing_3662.jsonl")
        def test_body_must_not_run():
            raise AssertionError("body ran")
        """,
        tmp_path / "events.jsonl",
    )
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*Replay fixture not found*"])
    # The decision-enabling assertion is about behaviour (body never ran),
    # not the message text (Tier 4) — an AssertionError from the body itself
    # would ALSO show as a failure, so confirm it's the fixture-setup path,
    # not the body, that failed.
    assert "AssertionError" not in "\n".join(result.outlines)


def test_missing_fixture_with_record_env_still_records(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Tier 1: #3662 bootstrap witness — REYN_LLM_RECORD=1 with a missing
    fixture still reaches the real call path (proven by a refused-loopback
    connection error, not the "fixture not found" failure, and not
    UnpinnedNetworkReach). If this test goes red, first-run fixture
    generation is broken."""
    monkeypatch.setenv("REYN_LLM_RECORD", "1")
    result = _run_inner(
        pytester,
        monkeypatch,
        """
        import pytest
        from reyn.dev.testing.network_gate import UnpinnedNetworkReach

        @pytest.mark.replay("fixtures/llm/nonexistent/missing_3662.jsonl")
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
    result.stdout.no_fnmatch_line("*Replay fixture not found*")
