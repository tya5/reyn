"""Tier 2: #3636 population 2 — config_reloaded adjacent-duplicate history text.

#3633 measured 27 adjacent exact-duplicate ``history.jsonl`` records in the
owner's real session; 24 (``router_tool_turn_text`` -> ``router_tool_turn``)
were fixed there. The remaining 3 were filed as #3636: 2 ``role=user`` (no
``meta.source``) and 1 ``role=system``/``config_watcher`` pair sharing the
SAME ``wal_seq``, 68ms apart.

**Root cause, established via the owner's real events log** (not static
reading alone — the ``.reyn/events/agents/default/chat/2026-07/
2026-07-21T071041.jsonl`` file, the only place this was directly observable):
the two adjacent duplicate ``config_watcher`` history records do NOT come
from a single write happening twice. The events log shows TWO DISTINCT,
correctly-emitted ``config_reloaded`` P6 events, 68ms apart, each following
its own ``pipeline_installed`` event with a DIFFERENT pipeline name
(``rag_ingest`` then ``rag_query`` — the FP-0063 RAG plugin installs two
pipelines in one ``plugin_install`` call, and ``pipeline_install.py``'s
per-pipeline ``dispatch_install_reload`` call correctly fires once per
pipeline). Nobody writes the same fact twice; ``notify_state_change``'s
``config_reloaded`` template only interpolated ``source`` (not which
pipeline/skill/server changed), so two GENUINELY DIFFERENT config changes
collapsed into byte-identical rendered text — an adjacent-duplicate-shaped
artifact of lost resolution, not a double-write.

The fix threads an optional ``detail`` (the specific entity name) from each
install call site, through ``HotReloader.apply_now``/``request_reload`` and
the emitted ``config_reloaded`` event, into the rendered summary — so two
different installs of the same ``source`` kind render distinguishably instead
of aliasing to the same string. This does NOT deduplicate anything: both
``config_reloaded`` events still land in history; the fix only restores the
resolution that made them look like a duplicate.

Population 1 (``role=user``, no ``meta.source``) is explicitly NOT addressed
here: re-measuring the owner's live ``history.jsonl`` (367 records as of this
investigation, up from 283) found 0 current adjacent duplicates for that
shape using the exact-match definition (all fields except timestamp equal) —
it does not currently reproduce, and no root cause is established. Per this
issue's instruction not to bundle a guess with a finding, this file covers
ONLY population 2.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.hot_reload import HotReloader, dispatch_install_reload
from reyn.runtime.session import _STATE_CHANGE_EVENT_MAPPINGS, ChatMessage, Session
from tests._support.agent_session import make_session
from tests._support.events import settle


def _make_session(tmp_path: Path, *, agent_name: str = "cfg-3636") -> Session:
    return make_session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / f"{agent_name}.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _state_changes(session: Session) -> list[ChatMessage]:
    return [
        m for m in session.history
        if m.role == "system" and (m.meta or {}).get("kind") == "state_change"
    ]


def _count_adjacent_exact_duplicates(history: list) -> int:
    """Count adjacent pairs with identical (role, content) — the shape
    measured for the owner's real ``history.jsonl`` (#3633 / #3636)."""
    count = 0
    for prev, cur in zip(history, history[1:]):
        if prev.role == cur.role and prev.content == cur.content:
            count += 1
    return count


# ── _format_config_reloaded contract (pure function) ────────────────────


def test_format_config_reloaded_without_detail_matches_pre_fix_text():
    """Tier 1: no ``detail`` (the pre-#3636 shape — operator/llm_op deferred
    reloads never set one) renders byte-identical to the original template,
    so existing callers see no behavior change."""
    source, formatter = _STATE_CHANGE_EVENT_MAPPINGS["config_reloaded"]
    assert source == "config_watcher"
    summary = formatter({"source": "operator", "components": [], "failed": []})
    assert summary == "Reyn configuration was hot-reloaded (source: operator)."


def test_format_config_reloaded_with_detail_distinguishes_entity():
    """Tier 1: a present ``detail`` is folded into the summary, so two
    different entities installed under the same ``source`` render
    differently."""
    _, formatter = _STATE_CHANGE_EVENT_MAPPINGS["config_reloaded"]
    ingest = formatter({"source": "pipeline_install", "detail": "rag_ingest"})
    query = formatter({"source": "pipeline_install", "detail": "rag_query"})
    assert ingest != query
    assert "rag_ingest" in ingest
    assert "rag_query" in query


# ── end-to-end: two distinct installs no longer alias ────────────────────


def test_two_distinct_pipeline_installs_render_distinct_history(tmp_path):
    """Tier 2: reproduces the owner's real sequence (#3636) — two DIFFERENT
    pipelines (``rag_ingest``, ``rag_query``) each installed via a pure-
    addition ``dispatch_install_reload`` call, mirroring
    ``pipeline_install.py``'s per-pipeline call. Post-fix: 0 adjacent exact
    duplicates, and the two ``config_watcher`` history entries carry distinct
    ``content`` naming each pipeline."""
    session = _make_session(tmp_path)
    reloader = HotReloader(project_root=tmp_path, events=session._audit_events)
    pre = len(_state_changes(session))

    async def _install_both() -> None:
        for name in ("rag_ingest", "rag_query"):
            await dispatch_install_reload(
                reloader, source="pipeline_install", is_addition=True, detail=name,
            )
        await settle(session._audit_events)

    asyncio.run(_install_both())

    # Tuple-unpack the NEW entries only: raises ValueError if there isn't
    # EXACTLY 2 — a behavioural assertion (both installs surfaced, nothing
    # collapsed/dropped), not a length pin.
    ingest_change, query_change = _state_changes(session)[pre:]
    assert ingest_change.content != query_change.content
    assert "rag_ingest" in ingest_change.content
    assert "rag_query" in query_change.content
    assert _count_adjacent_exact_duplicates(session.history) == 0


def test_two_installs_without_detail_reproduce_the_measured_duplicate(tmp_path):
    """Tier 2: RED-shape regression guard — reproduces the PRE-FIX byte-
    identical collision directly (not by reverting the fix): two pipeline
    installs that omit ``detail`` (as EVERY call site did before #3636)
    still alias to the same history text. This is not a bug in the current
    code (every real call site now passes ``detail``) — it pins that the
    formatter's backward-compatible no-``detail`` path is EXACTLY the shape
    that produced the owner's measured duplicate, so the fix's mechanism
    (adding ``detail``) is the thing closing the gap, not an unrelated
    change."""
    session = _make_session(tmp_path)
    reloader = HotReloader(project_root=tmp_path, events=session._audit_events)

    async def _install_twice() -> None:
        for _ in range(2):
            await dispatch_install_reload(
                reloader, source="pipeline_install", is_addition=True,
            )
        await settle(session._audit_events)

    asyncio.run(_install_twice())

    changes = _state_changes(session)
    last_two = changes[-2:]
    assert last_two[0].content == last_two[1].content
    assert _count_adjacent_exact_duplicates(session.history) == 1
