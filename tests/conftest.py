"""Pytest configuration for the Reyn test suite.

Registers the ``@pytest.mark.replay(fixture_rel)`` marker and provides the
``_llm_replay`` autouse fixture that wires it up.

Usage in tests::

    @pytest.mark.replay("fixtures/llm/skill_router/chitchat.jsonl")
    def test_router_chitchat(_llm_replay):
        ...

Environment variables
---------------------
``REYN_LLM_RECORD=1``
    Force record mode — call the real LLM and overwrite / extend the fixture.
    Requires a live LLM backend (see ``project_local_env.md`` in memory).

Record mode is also activated automatically when a fixture file is missing
(first-run bootstrap).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# ── Marker registration ────────────────────────────────────────────────────────


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "replay(fixture): monkeypatch litellm.acompletion with a JSONL fixture. "
        "Pass the fixture path relative to the tests/ directory.",
    )


# ── Autouse fixture ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _llm_replay(request: pytest.FixtureRequest):
    """Install / restore the LLM replay mock for tests marked with @replay."""
    marker = request.node.get_closest_marker("replay")
    if marker is None:
        # Not a replay test — let the real litellm through (or let the test
        # mock it however it likes).
        yield
        return

    fixture_rel: str = marker.args[0]
    fixture_path = Path(__file__).parent / fixture_rel

    force_record = os.environ.get("REYN_LLM_RECORD") == "1"
    mode: str
    if force_record:
        mode = "record"
    elif not fixture_path.exists():
        # First-run: no fixture yet — record automatically.
        mode = "record"
    else:
        mode = "replay"

    from reyn.testing.replay import LLMReplay

    replay = LLMReplay(fixture_path, mode=mode)  # type: ignore[arg-type]
    replay.install()
    try:
        yield replay
    finally:
        replay.restore()
        if mode == "record":
            replay.flush()
