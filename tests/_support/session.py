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
from reyn.runtime.agent import Agent
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.recovery import build_recovery
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


def make_session(
    tmp_path: Path,
    *,
    t_max: int = 1_000_000,
    agent: Agent | None = None,
    state_log: StateLog | None = None,
    snapshot_path: Path | None = None,
) -> Session:
    """Create a Session whose compaction engine uses a synthetic T_max.

    ``use_chars4_estimate=True`` makes token estimation deterministic:
    each character counts as 1/4 token.

    ``t_max`` is injected via monkeypatch so effective_trigger is
    predictable in tests.  The default (1_000_000) is large enough that
    any realistic test conversation fits and no elide fires, unless a
    smaller t_max is passed.

    ``agent`` / ``state_log`` / ``snapshot_path`` are optional explicit
    overrides — pass one to build a Session around a caller-controlled
    identity or recovery pair. Any omitted, defaults to a fixed
    ``tmp_path``-derived construction (below), same as before #3413.
    (#3413: prior to this, the three were built unconditionally inside the
    helper with no override kwarg at all — not a default that could silently
    absorb a caller-supplied value (structurally impossible to pass one),
    but an AST enumeration of this helper's 15 call sites across 4 files
    found one, ``test_skill_invoke_3100.py::_session_with_skills``, that had
    to duplicate this entire function body verbatim just to thread an extra
    ``capability_scope`` kwarg through — evidence the missing override path
    was already forcing copy-paste rather than a hazard someone had
    silently hit. What breaks if these three stay override-less: any future
    caller needing a specific ``agent`` / ``state_log`` / ``snapshot_path``
    duplicates the helper instead of parameterizing it, same as
    ``test_skill_invoke_3100.py`` did. Making them explicit optional
    overrides removes that duplication incentive without changing behaviour
    for the 15 existing call sites, none of which pass any of the three.)
    """
    if agent is None:
        # Agent is the sole identity SSoT (#3133 Priority-0 step-2 removed
        # the flat identity kwargs Session used to also accept alongside
        # ``agent=``).
        agent = Agent(agent_name="default", role="")
    if state_log is None:
        state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    bt = BudgetTracker(CostConfig())
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,  # deterministic: chars // 4
        section_caps_spec_tokens=0,  # keeps B_M positive for small T_max values
    )
    if snapshot_path is None:
        snapshot_path = (
            tmp_path / ".reyn" / "agents" / agent.agent_name / "state" / "snapshot.json"
        )
    # Session no longer builds its own recovery pair (generation_store ->
    # journal) — build it here from the same inputs the pre-refactor
    # Session.__init__ read internally (recovery-bundle-out-of-Session).
    generation_store, journal = build_recovery(
        agent.agent_name, snapshot_path, state_log, "main",
    )
    # Monkeypatch covers the engine's compute_budgets() call at Session init.
    with synthetic_t_max(t_max):
        return Session(
            agent=agent,
            generation_store=generation_store,
            journal=journal,
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            compaction_config=cfg,
            snapshot_path=snapshot_path,
        )


def push(session: Session, role: str, text: str) -> None:
    if role == "agent":
        role = "assistant"
    session.history.append(ChatMessage(role=role, content=text, ts=now()))
