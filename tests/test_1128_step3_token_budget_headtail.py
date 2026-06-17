"""Tier 2: #1128 step 3 — token-budget head/tail elide contract.

Two tests pin the new behaviours introduced in #1128 step 3:

1. Deprecation warning: loading a YAML config with ``chat.compaction.head_size``
   or ``chat.compaction.tail_size`` emits a ``DeprecationWarning``.

2. Token-budget elide contract for ``_build_history_for_router``:
   - Small chat (total tokens < effective_trigger): ALL turns returned raw,
     no elide, no duplication.
   - Large chat (total tokens > effective_trigger): middle turns elided;
     head and tail present, at least one middle turn absent.

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


def _make_session_with_t_max(tmp_path: Path, t_max: int):
    """Return a Session with a synthetic T_max.

    ``section_caps_spec_tokens=0`` keeps B_M positive for small T_max.
    ``use_chars4_estimate=True`` makes token counting deterministic.
    """
    from reyn.chat.session import Session
    from reyn.config import CompactionConfig
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig

    original = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: t_max  # type: ignore[assignment]
    try:
        session = Session(
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
    finally:
        _mb.get_max_input_tokens = original
    return session


def _push(session, role: str, content: str) -> None:
    from reyn.chat.session import ChatMessage
    if role == "agent":
        role = "assistant"
    session.history.append(ChatMessage(role=role, content=content, ts=_now()))


# Content that yields 80 tokens (320 chars / 4) via use_chars4_estimate=True.
# With T_max=2000 (T_SP≈1125, T_comp_SP≈481, section_caps=0):
#   effective_trigger≈570, head_budget≈87, tail_budget≈131.
# 3 turns × 80 tokens = 240 < 570 → no elide.
# 8 turns × 80 tokens = 640 > 570 → elide fires.
_CONTENT_80TOK = "X" * 320


def test_build_history_small_chat_returns_all_turns_raw(tmp_path) -> None:
    """Tier 2: a small chat (total tokens < effective_trigger) returns ALL turns
    without elide, and no turn appears more than once.

    This pins the window-utilization-first contract (#1128 step 3 Fork B):
    the LLM sees the full raw conversation as long as it fits under the
    trigger threshold.  No duplication can occur from this branch.
    """
    # T_max=2000 → effective_trigger≈570.  3 turns × 80 tokens = 240 < 570.
    session = _make_session_with_t_max(tmp_path, t_max=2000)
    for text in ["alpha", "beta", "gamma"]:
        _push(session, "user", text)

    msgs = session._build_history_for_router()
    contents = [m["content"] for m in msgs]

    assert contents == ["alpha", "beta", "gamma"], (
        "small chat must return all turns in order — no elide"
    )
    assert len(set(contents)) == len(contents), (
        "window-utilization branch must not duplicate turns"
    )


def test_build_history_large_chat_elides_middle(tmp_path) -> None:
    """Tier 2: a large chat (total tokens > effective_trigger) elides the middle —
    head is present, tail is present, and at least one middle turn is absent.

    Uses T_max=2000 with 30 turns of 80-token content (total=2400 tokens).
    30×80=2400 exceeds any effective_trigger for T_max=2000 regardless of the
    SP size (effective_trigger < T_max by construction), making this test
    default-independent: changing hot_list_n or other SP-affecting defaults
    does not change whether elide fires.
    """
    session = _make_session_with_t_max(tmp_path, t_max=2000)
    texts = [f"turn-{i}:" + _CONTENT_80TOK for i in range(30)]
    for i, text in enumerate(texts):
        _push(session, "user" if i % 2 == 0 else "assistant", text)

    msgs = session._build_history_for_router()
    contents = [m["content"] for m in msgs]
    present = set(contents)

    # Head and tail turns must survive the elide.
    assert texts[0] in present, "first turn (head) must be present after elide"
    assert texts[-1] in present, "last turn (tail) must be present after elide"

    # At least one middle turn must be absent (= elide actually fired).
    middle_absent = any(t not in present for t in texts[1:-1])
    assert middle_absent, (
        "expected at least one middle turn elided, but all turns present — "
        "elide branch did not fire; check that total > effective_trigger"
    )

    # No duplicates — the overlap deduplication guard must hold.
    assert len(contents) == len(set(contents)), (
        "duplicate messages in router view — elide overlap deduplication failed"
    )
