"""#3338 — TUI status chrome: read the snapshot keys the chrome dropped, and
keep the status line + an open drawer pane LIVE.

When the prompt_toolkit inline app was retired (``b473cbf4``, #3273 Phase 6) its
DATA function (``_snapshot``) was preserved into ``interfaces/repl/status.py``
but its RENDERING was deleted rather than migrated. The result was not "the data
is gone" — every key is still produced — but "the surface stopped reading them":
``textual_chat/chrome.py``'s formatters never touched ``cost_breakdown_*`` /
``session_cached_tokens`` / ``ctx_recent_usage`` / ``ctx_compaction_status_fn`` /
``session_tree``, and six whole categories (tool / mcp / skill visibility, hook,
pipeline, cron) had no tab at all. Two liveness defects sat on the same surface:
the collapsed status line refreshed only on DISPLAY frames (EVENT frames — which
is what an LLM call is — took a ``continue`` past it), and a drawer pane was
built once at open time and then froze.

What this file gates:

1. **Producer↔consumer key compatibility** (the anti-#3037 gate): the snapshot a
   REAL ``AgentRegistry``/``Session`` produces carries every key the formatters
   read, and every formatter runs against that real snapshot. A fake dict that
   invented a field would make a dead read look tested; here the dict comes from
   the real producer and only its VALUES are substituted (with the real types
   the producer itself stores there — real ``CostBreakdown`` instances, real
   ``(int, int)`` usage tuples, real ``session_tree``-shaped dicts).
2. **Non-vacuity per pane**: for each restored key, a value carried in the
   snapshot must appear in the pane's output.
3. **The three cost states stay three**: ``ok`` / ``approx`` (>200k tiered
   pricing, ``~`` + footnote) / ``unavail`` (breakdown absent, ``—`` + a
   DIFFERENT footnote) render distinguishably. Collapsing ``unavail`` into
   ``approx`` is the misattribution false-fire an architect caught once already.
4. **Operability, not just display**: a restored category's row carries the
   ``/visibility`` / ``/hook`` slash that flips it, and selecting the row in the
   MOUNTED app actually submits that slash through the transport.
5. **Liveness**: pushing ONLY ``FrameTag.EVENT`` frames (never a DISPLAY frame)
   moves the status-line text, and an OPEN pane's content, while a pane that is
   NOT open stays untouched — the bound that keeps the expensive lazy
   ``ctx_compaction_status_fn`` off the per-frame path.

Real ``AgentRegistry`` / ``Session`` / ``TextualChatApp`` / ``ClientTransport``
throughout — no ``unittest.mock``.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import AsyncIterator

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat.chrome import (
    _MENU_TABS,
    cost_pane_lines,
    ctx_pane_lines,
    pane_commands,
    pane_payload,
    status_line_text,
)
from reyn.interfaces.repl.read_model import ChatReadModel
from reyn.interfaces.repl.status import _snapshot
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import EventFrame
from reyn.llm.pricing import CostBreakdown
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.schemas.models import Event
from tests._support.agent_session import make_session

AGENT = "chrome-3338-agent"


# ── real producer: a real registry + attached session -> a real _snapshot() ───


async def _real_snapshot(tmp_path: Path) -> "tuple[dict, Session, AgentRegistry]":
    """A snapshot dict produced by the REAL ``interfaces/repl/status.py``
    ``_snapshot()`` off a REAL attached ``Session``. Every key/typed value below
    therefore comes from production code, never from a hand-written literal."""
    state_log = StateLog(tmp_path / "state.wal")

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name,
            state_log=state_log,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
        )

    registry = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log
    )
    AgentProfile.new(AGENT, role="").save(tmp_path / ".reyn" / "agents" / AGENT)
    session = await registry.attach(AGENT)
    snap = _snapshot(registry)
    assert snap is not None, "the real producer returned no snapshot"
    return snap, session, registry


#: Every snapshot key the restored chrome formatters read. Asserted against the
#: REAL producer's output below, so a key renamed/removed upstream fails here
#: rather than silently degrading a pane to its zero fallback.
_KEYS_THE_CHROME_READS = frozenset({
    "usage", "agent_tokens", "cost_usd", "cost_agent", "cost_total",
    "cost_breakdown_session", "cost_breakdown_agent", "cost_breakdown_project",
    "session_cached_tokens",
    "ctx_used", "ctx_window", "ctx_source", "ctx_recent_usage",
    "ctx_compaction_status_fn",
    "session_tree", "agent_names", "attached_name",
    "model", "model_classes", "model_active_class",
    "visibility_items", "hook_items", "hooks", "pipelines", "cron_jobs",
    "mcp_servers", "skills",
})


@pytest.mark.asyncio
async def test_real_snapshot_carries_every_key_the_chrome_reads(tmp_path) -> None:
    """Tier 2: the REAL ``_snapshot()`` produces every key the restored chrome
    formatters read — the producer↔consumer contract, checked against the real
    producer rather than a hand-written dict that could invent a field (#3037).
    Also runs each formatter against that real snapshot, so a key present but
    carrying an unexpected TYPE fails here too."""
    snap, _session, _registry = await _real_snapshot(tmp_path)

    missing = _KEYS_THE_CHROME_READS - set(snap)
    assert not missing, f"chrome reads keys the real producer does not emit: {missing}"

    # Every formatter must survive the real snapshot's real values.
    for tab_id, _label in _MENU_TABS:
        rows = pane_payload(tab_id, snapshot=snap)
        assert isinstance(rows, list), f"{tab_id} pane did not produce rows"
    assert status_line_text(snap, "fallback")


# ── Cost pane: the three scopes, the three states, the savings denominator ────


def _saved_pct_row(lines: "list[str]") -> str:
    return next(line for line in lines if line.startswith("Saved%"))


@pytest.mark.asyncio
async def test_cost_pane_surfaces_all_three_scope_breakdowns(tmp_path) -> None:
    """Tier 2: a value carried in EACH of the three ``cost_breakdown_*`` keys
    surfaces in the Cost pane — the regression was that the pane read none of
    them (only ``cost_agent``/``cost_total``), so the Session scope and every
    Input/Output/Saved row were simply absent.

    Non-vacuity: the three scopes carry DISTINCT figures, so a formatter that
    rendered one scope three times would fail."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    snap["cost_breakdown_session"] = CostBreakdown(
        prompt_cost=0.0100, completion_cost=0.0200, cache_savings=0.0300
    )
    snap["cost_breakdown_agent"] = CostBreakdown(
        prompt_cost=0.0400, completion_cost=0.0500, cache_savings=0.0600
    )
    snap["cost_breakdown_project"] = CostBreakdown(
        prompt_cost=0.0700, completion_cost=0.0800, cache_savings=0.0900
    )
    snap["cost_usd"] = 0.0300
    snap["cost_agent"] = 0.0900
    snap["cost_total"] = 0.1500

    blob = "\n".join(cost_pane_lines(snap))
    for amount in (
        "$0.0100", "$0.0200", "$0.0300",   # Session Input / Output / Saved
        "$0.0400", "$0.0500", "$0.0600",   # Agent
        "$0.0700", "$0.0800", "$0.0900",   # Project
    ):
        assert amount in blob, f"cost pane never surfaced {amount}:\n{blob}"
    assert "$0.1500" in blob, "the Project authoritative Total is missing"


@pytest.mark.asyncio
async def test_saved_pct_denominator_is_input_plus_saved_not_total(tmp_path) -> None:
    """Tier 2: Saved% = ``Saved / (Input + Saved)`` — the no-cache baseline, i.e.
    what input WOULD have cost without caching — never ``Saved / Total``. The two
    denominators are chosen here to give visibly different answers (20% vs 10%),
    so pinning the wrong one silently understating the savings rate is RED."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    breakdown = CostBreakdown(
        prompt_cost=0.0080, completion_cost=0.0120, cache_savings=0.0020
    )
    snap["cost_breakdown_session"] = breakdown
    snap["cost_breakdown_agent"] = CostBreakdown()
    snap["cost_breakdown_project"] = CostBreakdown()
    snap["cost_usd"] = 0.0200      # Input 0.0080 + Output 0.0120 -> "ok"
    snap["cost_agent"] = 0.0
    snap["cost_total"] = 0.0

    row = _saved_pct_row(cost_pane_lines(snap))
    assert "20%" in row, f"Saved% is not Saved/(Input+Saved): {row}"
    assert "10%" not in row, f"Saved% used the Saved/Total denominator: {row}"


def _cost_blob(snap: dict) -> str:
    return "\n".join(cost_pane_lines(snap))


@pytest.mark.asyncio
async def test_cost_states_ok_approx_unavail_all_render_distinctly(tmp_path) -> None:
    """Tier 2: the THREE cost-breakdown states stay three, and ``unavail`` is
    never dressed as ``approx``.

    - ``ok``      — components reconcile with the authoritative Total: exact cells.
    - ``approx``  — components present but diverging = real >200k tiered pricing:
                    ``~``-marked cells + the tiered-pricing footnote.
    - ``unavail`` — components ~0 while Total > 0 = the in-memory breakdown was
                    never accumulated / reset on restart: ``—`` cells + a DIFFERENT
                    footnote, and explicitly NOT the tiered one.

    Collapsing the last two into one state misattributes a missing breakdown to
    tiered pricing — the false-fire this assertion exists to keep RED."""
    base, _session, _registry = await _real_snapshot(tmp_path)

    ok = copy.copy(base)
    ok["cost_breakdown_session"] = CostBreakdown(
        prompt_cost=0.0100, completion_cost=0.0100
    )
    ok["cost_usd"] = 0.0200
    ok["cost_agent"] = 0.0
    ok["cost_total"] = 0.0

    approx = copy.copy(base)
    approx["cost_breakdown_session"] = CostBreakdown(
        prompt_cost=0.0100, completion_cost=0.0100
    )
    approx["cost_usd"] = 0.0500  # components present, do NOT reconcile -> tiered
    approx["cost_agent"] = 0.0
    approx["cost_total"] = 0.0

    unavail = copy.copy(base)
    unavail["cost_breakdown_session"] = CostBreakdown()  # nothing accumulated
    unavail["cost_usd"] = 0.0500  # but a real, authoritative Total exists
    unavail["cost_agent"] = 0.0
    unavail["cost_total"] = 0.0

    ok_blob, approx_blob, unavail_blob = (
        _cost_blob(ok), _cost_blob(approx), _cost_blob(unavail)
    )

    assert ok_blob != approx_blob != unavail_blob != ok_blob, (
        "two of the three cost states render identically"
    )
    assert "~" not in ok_blob and "tiered" not in ok_blob, (
        f"a reconciling breakdown was marked approximate:\n{ok_blob}"
    )
    assert "~$0.0100" in approx_blob and "tiered pricing" in approx_blob, (
        f"genuine >200k tiering was not marked approximate:\n{approx_blob}"
    )
    assert "tiered" not in unavail_blob, (
        f"an UNAVAILABLE breakdown was misattributed to tiered pricing:\n{unavail_blob}"
    )
    assert "unavailable" in unavail_blob, (
        f"an unavailable breakdown got no distinct note:\n{unavail_blob}"
    )
    assert "$0.0500" in unavail_blob, "the authoritative Total must stay exact"


@pytest.mark.asyncio
async def test_cost_pane_surfaces_cumulative_cache_hit(tmp_path) -> None:
    """Tier 2: ``session_cached_tokens`` (against the cumulative prompt tokens)
    reaches the Cost pane's cache line, marked cumulative to distinguish it from
    the Ctx pane's last-call cache figure. The pane previously read neither."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    snap["usage"] = (12345, 6789, 19134)
    snap["agent_tokens"] = 19134
    snap["session_cached_tokens"] = 5180

    blob = _cost_blob(snap)
    assert "5,180" in blob, f"session_cached_tokens never surfaced:\n{blob}"
    assert "42% hit" in blob, f"cumulative cache-hit rate missing:\n{blob}"
    assert "cumulative" in blob, "the cache line must mark itself cumulative"
    assert "12,345" in blob and "6,789" in blob, "token counters missing"


# ── Ctx pane: free tokens, last-call cache, the compaction estimate ───────────


@pytest.mark.asyncio
async def test_ctx_pane_surfaces_free_tokens_and_last_call_cache(tmp_path) -> None:
    """Tier 2: the Ctx pane surfaces the window SOURCE, the free-token headroom,
    and the LAST CALL's cache hit (``ctx_recent_usage``) — three figures the pane
    stopped reading. ``free`` is derived, so a pane that only echoed used/window
    (the regressed shape) never shows it."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    snap["ctx_window"] = 200000
    snap["ctx_used"] = 48120
    snap["ctx_recent_usage"] = (48120, 14900)

    blob = "\n".join(ctx_pane_lines(snap))
    assert "151,880" in blob, f"free-token headroom missing:\n{blob}"
    assert "14,900" in blob and "31% hit" in blob, (
        f"last-call cache hit (ctx_recent_usage) missing:\n{blob}"
    )
    assert snap["ctx_source"] in blob, "the window's source attribution is missing"
    assert "24% of window" in blob, "prompt occupancy percent missing"


@pytest.mark.asyncio
async def test_ctx_pane_surfaces_the_real_compaction_estimate(tmp_path) -> None:
    """Tier 2: the compaction block comes from ``ctx_compaction_status_fn`` — the
    REAL bound ``Session.context_window_status`` the snapshot stores UNCALLED —
    and is kept visually separate from the window/prompt/free block above it (it
    measures a different thing against a different threshold).

    Non-vacuity: the asserted trigger figure is read back off the real session, so
    it cannot be satisfied by a hardcoded string."""
    snap, session, _registry = await _real_snapshot(tmp_path)
    status = session.context_window_status()
    trigger = status["effective_trigger"]
    assert trigger > 0, "the real session reported no compaction trigger to gate on"

    blob = "\n".join(ctx_pane_lines(snap))
    assert f"{trigger:,}" in blob, f"the real compaction trigger never surfaced:\n{blob}"
    assert "to trigger" in blob, "the compaction block is missing its framing"


def test_ctx_percent_is_dash_before_any_completed_call() -> None:
    """Tier 1: with no completed LLM call (``used``/``window`` still 0) the
    context percent is ``—``, never a misleading ``0%`` — a real completed call's
    prompt_tokens is never actually 0 (the system prompt alone is nonzero), so a
    literal 0% would read as "empty context" rather than "no data yet"."""
    assert "—" in status_line_text({"ctx_window": 0, "ctx_used": 0}, "a")
    assert "0%" not in status_line_text({"ctx_window": 200000, "ctx_used": 0}, "a")
    assert "45%" in status_line_text({"ctx_window": 200000, "ctx_used": 90000}, "a")


# ── Agent pane: the session tree beneath each agent ───────────────────────────


@pytest.mark.asyncio
async def test_agent_pane_shows_the_real_session_tree_and_can_switch(tmp_path) -> None:
    """Tier 2: the Agent pane reads ``session_tree`` (a REAL
    ``AgentRegistry.session_tree()``), so the sessions beneath an agent are
    visible AND reachable — the regressed pane rendered a flat agent-name list, so
    a session could be neither seen nor switched to."""
    snap, _session, registry = await _real_snapshot(tmp_path)
    tree = snap["session_tree"]
    assert tree and tree[0]["sessions"], "the real registry exposed no session tree"
    sid = tree[0]["sessions"][0]["sid"]

    rows = pane_payload("agent", snapshot=snap)
    cmds = pane_commands("agent", snap)
    assert any(sid in row for row in rows), f"session {sid!r} not visible in {rows}"
    assert len(cmds) == len(rows), "agent rows and their commands drifted apart"
    assert f"/session switch {sid}" in cmds, (
        f"the attached agent's session row is not switchable: {cmds}"
    )
    assert f"/attach {AGENT}" in cmds, "the agent row is not attachable"


# ── The six restored categories: present AND operable ─────────────────────────


@pytest.mark.asyncio
async def test_all_six_restored_categories_have_a_tab(tmp_path) -> None:
    """Tier 1: tool / mcp / skill / pipe / hook / cron each have a drawer tab
    again — they were reachable behind the retired chip bar's ``more…`` sub-bar
    and had no surface at all after the port."""
    tab_ids = {tid for tid, _label in _MENU_TABS}
    assert {"tool", "mcp", "skill", "pipe", "hook", "cron"} <= tab_ids, (
        f"restored categories missing from the tab row: {sorted(tab_ids)}"
    )


@pytest.mark.asyncio
async def test_visibility_rows_carry_the_flipping_visibility_slash(tmp_path) -> None:
    """Tier 2: each tool-visibility row carries the ``/visibility`` slash that
    FLIPS its current state (an ``on`` item dispatches ``off`` and vice versa) —
    display without operability was explicitly not acceptable. The items are the
    REAL ``capability_visibility_state()`` projection off the attached session."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    items = [it for it in snap["visibility_items"] if it["kind"] == "tool"]
    assert items, "the real session exposed no tool-visibility items to gate on"

    rows = pane_payload("tool", snapshot=snap)
    cmds = pane_commands("tool", snap)
    assert len(rows) == len(cmds) == len(items), "tool rows and commands drifted"
    for item, row, cmd in zip(items, rows, cmds):
        assert item["name"] in row, f"{item['name']} missing from its row {row!r}"
        want = "off" if item["on"] else "on"
        assert cmd == f"/visibility {want} tool {item['name']}", (
            f"row for {item['name']} does not flip its state: {cmd!r}"
        )


def test_hook_rows_carry_the_flipping_hook_slash() -> None:
    """Tier 1: a hook-applicability row dispatches ``/hook on|off <name>``,
    flipping the current state — the same operability contract as the visibility
    categories. The item shape is ``_session_hook_items``'s documented
    ``{name, scope, on}`` projection."""
    snap = {"hook_items": [
        {"name": "pre_tool_guard", "scope": "project", "on": True},
        {"name": "post_turn_notify", "scope": "", "on": False},
    ]}
    rows = pane_payload("hook", snapshot=snap)
    cmds = pane_commands("hook", snap)
    assert any("pre_tool_guard" in r and "project" in r for r in rows), rows
    assert cmds == ["/hook off pre_tool_guard", "/hook on post_turn_notify"], cmds


def test_pipe_and_cron_panes_surface_their_entries() -> None:
    """Tier 1: the read-only pipeline and cron categories list their entries
    (neither has an on/off toggle mechanism, so they carry no command)."""
    snap = {
        "pipelines": [{"name": "nightly", "description": "the nightly pass"}],
        "cron_jobs": [{"name": "sweep", "schedule": "0 3 * * *", "enabled": True}],
    }
    assert any("nightly" in r for r in pane_payload("pipe", snapshot=snap))
    pipe_rows = " ".join(pane_payload("pipe", snapshot=snap))
    assert "the nightly pass" in pipe_rows
    cron_rows = " ".join(pane_payload("cron", snapshot=snap))
    assert "sweep" in cron_rows and "0 3 * * *" in cron_rows and "[on]" in cron_rows


# ── App wiring: liveness + reachable dispatch on the MOUNTED app ──────────────


class _MutableSnapshotReadModel(ChatReadModel):
    """A real :class:`ChatReadModel` seam impl (like ``RegistryReadModel`` /
    ``RemoteReadModel``) returning a snapshot dict the test can MUTATE between
    frames — standing in for a live session whose cost/ctx move as a turn runs,
    without needing a real LLM call. The dict itself is produced by the real
    ``_snapshot()``."""

    def __init__(self, snap: dict) -> None:
        self.snap = snap

    def snapshot(self, config=None):
        return self.snap

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    def clear_pending_command_ui(self) -> None:
        return None

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self) -> Path:
        return Path("/tmp/reyn_3338_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        return []


class _EventOnlyTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` fed one frame at a time. The
    liveness tests push ONLY ``EventFrame``s through it — never a DisplayFrame —
    so a status refresh that still sat on the DISPLAY leg can never be reached by
    the positive control."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()
        self.submitted: list[str] = []

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _llm_response_event() -> Event:
    """An EVENT-path frame of exactly the kind that moves cost/ctx mid-turn."""
    return Event(type="tool_returned", data={"tool": "read_file"})


@pytest.mark.asyncio
async def test_status_line_refreshes_on_event_frames_alone(tmp_path) -> None:
    """Tier 2: the collapsed status line tracks cost/ctx when ONLY ``FrameTag.EVENT``
    frames arrive — no DISPLAY frame is pushed at any point.

    This is the exact regression: ``_refresh_status()`` sat below the EVENT
    branch's ``continue``, so a turn whose whole tool-loop is event traffic (an
    LLM call landing, a tool returning) left the status line frozen at whatever
    the last display frame set. Mixing in a DISPLAY frame would make the positive
    control pass through the OLD path too, so this test deliberately never does."""
    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp

    snap, _session, _registry = await _real_snapshot(tmp_path)
    snap["cost_agent"] = 0.0100
    read_model = _MutableSnapshotReadModel(snap)
    transport = _EventOnlyTransport()
    app = TextualChatApp(transport=transport, read_model=read_model)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = str(app.query_one(StatusLine).render())
        assert "$0.0100" in before, f"initial status line lacks the cost: {before}"

        # The turn progresses: cost moves, and ONLY event frames report it.
        read_model.snap["cost_agent"] = 0.9999
        await transport.push_event(_llm_response_event())
        await pilot.pause()
        await pilot.pause()

        after = str(app.query_one(StatusLine).render())
        assert "$0.9999" in after, (
            f"status line did not refresh on an EVENT-only frame: {after}"
        )
        assert after != before, "status line text did not change at all"


@pytest.mark.asyncio
async def test_open_pane_updates_on_frame_and_closed_panes_do_not(tmp_path) -> None:
    """Tier 2: a drawer pane left OPEN keeps updating as frames land (it used to
    be built exactly once, at open time, and then froze) — AND a pane that is NOT
    open is left alone.

    The second half is the load-bearing bound, not a nicety: the Ctx pane's
    compaction row calls ``ctx_compaction_status_fn`` (=
    ``Session.context_window_status()``, a json.dumps + token estimate of the
    whole router-view history), which ``_snapshot()`` deliberately stores UNCALLED
    so it never runs per render frame. Rebuilding every pane on every frame would
    reinstate exactly the cost that seam exists to avoid."""
    from textual.widgets import Static

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    snap, _session, _registry = await _real_snapshot(tmp_path)
    snap["cost_agent"] = 0.0100
    snap["cost_usd"] = 0.0100
    snap["ctx_window"] = 200000
    snap["ctx_used"] = 10000
    read_model = _MutableSnapshotReadModel(snap)
    transport = _EventOnlyTransport()
    app = TextualChatApp(transport=transport, read_model=read_model)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app._open_drawer("cost")
        await pilot.pause()
        cost_before = str(app.query_one("#cost", Static).render())
        ctx_before = str(app.query_one("#ctx", Static).render())
        assert "$0.0100" in cost_before, cost_before

        read_model.snap["cost_usd"] = 0.7777
        read_model.snap["ctx_used"] = 190000
        await transport.push_event(_llm_response_event())
        await pilot.pause()
        await pilot.pause()

        cost_after = str(app.query_one("#cost", Static).render())
        assert "$0.7777" in cost_after, (
            f"an OPEN pane did not update on frame arrival: {cost_after}"
        )
        ctx_after = str(app.query_one("#ctx", Static).render())
        assert ctx_after == ctx_before, (
            "a pane that is not open was rebuilt — the lazy compaction-status "
            f"bound is gone: {ctx_after}"
        )


@pytest.mark.asyncio
async def test_selecting_a_visibility_row_dispatches_the_slash(tmp_path) -> None:
    """Tier 2: the restored categories are OPERABLE from the mounted app —
    selecting a Tool row routes its ``/visibility`` slash through the same
    transport seam a typed slash uses, then collapses the drawer. Rows that
    render but dispatch nothing were explicitly not acceptable."""
    from textual.widgets import ContentSwitcher, OptionList

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    snap, _session, _registry = await _real_snapshot(tmp_path)
    items = [it for it in snap["visibility_items"] if it["kind"] == "tool"]
    assert items, "the real session exposed no tool-visibility items to gate on"
    target = items[0]

    transport = _EventOnlyTransport()
    app = TextualChatApp(
        transport=transport, read_model=_MutableSnapshotReadModel(snap)
    )
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app._open_drawer("tool")
        await pilot.pause()
        option_list = app.query_one("#tool", OptionList)
        option_list.post_message(
            OptionList.OptionSelected(option_list, option_list.get_option_at_index(0), 0)
        )
        await pilot.pause()
        want = "off" if target["on"] else "on"
        assert f"/visibility {want} tool {target['name']}" in transport.submitted, (
            f"the Tool row dispatched nothing: {transport.submitted}"
        )
        assert app.query_one("#drawer", ContentSwitcher).display is False
