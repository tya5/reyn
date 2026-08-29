"""Tier 2: #1128 step 3 — token-budget head/tail elide contract.

Two tests pin the new behaviours introduced in #1128 step 3:

1. Deprecation warning: loading a YAML config with ``chat.compaction.head_size``
   or ``chat.compaction.tail_size`` emits a ``DeprecationWarning``.

2. The window-utilization case for ``build_history``: a small chat (total
   tokens well under any real trigger) returns ALL turns raw, no
   duplication. #5367 (owner ruling) retired the OTHER half of this
   contract — the large-chat elide-the-middle branch — entirely; see
   ``RouterHistoryBuffer.build_history``'s own docstring for why. Only
   the surviving half is pinned here now.

Policy compliance:
- No unittest.mock.
- No private-state assertions.
- Docstrings start with ``Tier 2: ``.
- Real config loader (not a mock) for the deprecation test.
- Real Session with monkeypatched ``get_max_input_tokens`` for the
  elide test (no mocked collaborators).
"""
from __future__ import annotations

import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest

import reyn.llm.model_budget as _mb
from tests._support.agent_session import make_session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Test 1: deprecation warning
# ---------------------------------------------------------------------------


def test_head_size_tail_size_emit_deprecation_warning() -> None:
    """Tier 2: loading a YAML config with ``chat.compaction.head_size`` or
    ``chat.compaction.tail_size`` emits a ``DeprecationWarning``.

    Uses the real ``_build_chat_config`` loader path — no mocks.  The old
    keys are silently ignored (head/tail sizing is now controlled by
    ``component_weights``); users must remove them to silence the warning.
    """
    from reyn.config import _build_chat_config  # noqa: PLC0415

    # Both keys present — must emit the warning.
    with pytest.warns(DeprecationWarning, match="deprecated and ignored"):
        cfg = _build_chat_config({
            "compaction": {
                "head_size": 6,
                "tail_size": 6,
                "body_token_cap": 1500,
            }
        })

    # The resulting CompactionConfig must NOT have head_size/tail_size fields.
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(cfg.compaction)}
    assert "head_size" not in field_names, (
        "CompactionConfig must not expose head_size after #1128 step 3 removal"
    )
    assert "tail_size" not in field_names, (
        "CompactionConfig must not expose tail_size after #1128 step 3 removal"
    )


def test_head_size_only_also_warns() -> None:
    """Tier 2: ``head_size`` alone (without ``tail_size``) also emits the deprecation."""
    from reyn.config import _build_chat_config  # noqa: PLC0415

    with pytest.warns(DeprecationWarning, match="deprecated and ignored"):
        _build_chat_config({"compaction": {"head_size": 12}})


@pytest.mark.parametrize("removed_key", ["trigger_total_tokens", "min_compact_batch"])
def test_axis1_config_keys_also_warn(removed_key) -> None:
    """Tier 2: #1128 PR-a — the axis-1 config keys ``trigger_total_tokens`` and
    ``min_compact_batch`` are removed too, so they warn symmetrically with
    ``head_size``/``tail_size`` (all four are operator-facing chat.compaction
    keys; none should silently ignore)."""
    from reyn.config import _build_chat_config  # noqa: PLC0415

    with pytest.warns(DeprecationWarning, match="deprecated and ignored"):
        _build_chat_config({"compaction": {removed_key: 2000}})


def test_clean_config_no_warning() -> None:
    """Tier 2: a config without ``head_size``/``tail_size`` emits no DeprecationWarning."""
    from reyn.config import _build_chat_config  # noqa: PLC0415

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        # Must not raise — no deprecated key present.
        _build_chat_config({"compaction": {"body_token_cap": 1500}})


# ---------------------------------------------------------------------------
# Test 2: _build_history_for_router token-budget elide contract
# ---------------------------------------------------------------------------


def _make_session_with_t_max(tmp_path: Path, monkeypatch, t_max: int):
    """Return a Session with a synthetic T_max.

    ``section_caps_spec_tokens=0`` keeps B_M positive for small T_max.
    ``use_chars4_estimate=True`` makes token counting deterministic.

    #3671 follow-up: takes the caller's ``monkeypatch`` fixture rather than
    manually saving/restoring ``get_max_input_tokens`` — CompactionEngine now
    builds LAZILY (``CompactionController._engine``, a property, on first
    reference) rather than eagerly at Session construction, so a patch that
    un-does itself the moment this function returns would go stale before
    the caller ever triggers the lazy build. ``monkeypatch`` restores at the
    CALLING TEST's teardown instead, keeping the patch live for whatever
    that test does with the session.
    """
    from reyn.config import CompactionConfig
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig

    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: t_max)
    return make_session(
        agent_name="default",
        agent_role="",
        output_language="en",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        compaction_config=CompactionConfig(
            use_chars4_estimate=True,
            section_caps_spec_tokens=0,
        ),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )


def _push(session, role: str, content: str) -> None:
    from reyn.runtime.chat_message import ChatMessage
    if role == "agent":
        role = "assistant"
    session.history.append(ChatMessage(role=role, content=content, ts=_now()))


def test_build_history_small_chat_returns_all_turns_raw(tmp_path, monkeypatch) -> None:
    """Tier 2: a small chat (total tokens < effective_trigger) returns ALL turns
    without elide, and no turn appears more than once.

    This pins the window-utilization-first contract (#1128 step 3 Fork B):
    the LLM sees the full raw conversation as long as it fits under the
    trigger threshold.  No duplication can occur from this branch.
    """
    # T_max=2800 → effective_trigger≈489.  3 turns × 80 tokens = 240 < 489.
    session = _make_session_with_t_max(tmp_path, monkeypatch, t_max=2800)
    for text in ["alpha", "beta", "gamma"]:
        _push(session, "user", text)

    msgs = session._history_buffer.build_history()
    contents = [m["content"] for m in msgs]

    assert contents == ["alpha", "beta", "gamma"], (
        "small chat must return all turns in order — no elide"
    )
    assert len(set(contents)) == len(contents), (
        "window-utilization branch must not duplicate turns"
    )
