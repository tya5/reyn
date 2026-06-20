"""Shared Session test builders for router-history / compaction tests.

``make_session`` creates a Session whose compaction engine uses a synthetic
T_max (injected via module-level replacement, the same pattern used in the
compaction tests — no unittest.mock).
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path

import reyn.llm.model_budget as _mb
from reyn.config import CompactionConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextlib.contextmanager
def synthetic_t_max(t_max: int):
    """Monkeypatch get_max_input_tokens for the duration of the with-block.

    Uses direct module-level replacement (the same pattern used in
    test_chat_compaction_engine_11axis.py) — no unittest.mock.
    """
    original = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: t_max  # type: ignore[assignment]
    try:
        yield
    finally:
        _mb.get_max_input_tokens = original


def make_session(tmp_path: Path, *, t_max: int = 1_000_000) -> Session:
    """Create a Session whose compaction engine uses a synthetic T_max.

    ``use_chars4_estimate=True`` makes token estimation deterministic:
    each character counts as 1/4 token.

    ``t_max`` is injected via monkeypatch so effective_trigger is
    predictable in tests.  The default (1_000_000) is large enough that
    any realistic test conversation fits and no elide fires, unless a
    smaller t_max is passed.
    """
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    bt = BudgetTracker(CostConfig())
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,  # deterministic: chars // 4
        section_caps_spec_tokens=0,  # keeps B_M positive for small T_max values
    )
    # Monkeypatch covers the engine's compute_budgets() call at Session init.
    with synthetic_t_max(t_max):
        return Session(
            agent_name="default",
            agent_role="",
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            compaction_config=cfg,
            snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
        )


def push(session: Session, role: str, text: str) -> None:
    if role == "agent":
        role = "assistant"
    session.history.append(ChatMessage(role=role, content=text, ts=now()))
