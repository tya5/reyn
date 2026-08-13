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
import re
from pathlib import Path
from typing import AsyncIterator

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat.chrome import (
    _COST_COL_W,
    _COST_LABEL_W,
    _MENU_TABS,
    _TAB_H_PADDING,
    MenuBar,
    StatusLine,
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
    therefore comes from production code, never from a hand-written literal.

    #3615: the session factory passes the registry back-reference (``holder``
    pattern, matching every unconditional ``build_scoped_chat_session`` production
    caller — chat.py / mcp.py) so ``capability_visibility_state``'s envelope axis
    is genuinely RESOLVED rather than reported ``unknown`` for want of a
    back-reference the real attach path always provides."""
    state_log = StateLog(tmp_path / "state.wal")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name,
            state_log=state_log,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
            registry=holder.get("reg"),
        )

    registry = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log
    )
    holder["reg"] = registry
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
    """The SESSION scope's row — Saved% is its last column since #3691
    transposed the table (scopes are rows now, metrics are columns)."""
    return next(line for line in lines if line.startswith("Session"))


def test_the_ctx_bar_sits_with_the_figure_it_draws() -> None:
    """Tier 2: the window bar is adjacent to the window percentage, not to the
    compaction one (#3691).

    The bar has always drawn the WINDOW's fill. It used to be printed after the
    compaction line — a 42% bar directly beneath "61% to trigger" — so the two
    figures the pane deliberately keeps separate were rendered as if one
    illustrated the other. Nothing about either number was wrong; the ADJACENCY
    was, which is not a property either line has on its own.

    Pinned by position rather than by the rendered strings: the claim is about
    which line the bar neighbours, and asserting the text would pin the
    formatting this test does not care about.
    """
    from reyn.interfaces.inline.textual_chat.chrome import ctx_pane_lines

    lines = ctx_pane_lines({
        "ctx_window": 128_000,
        "ctx_used": 54_000,
        "ctx_source": "model catalog",
        "ctx_recent_usage": (57_386, 18_000),
        "ctx_compaction_status_fn": lambda: {
            "effective_trigger": 51_000, "free_window": 20_000,
        },
    })
    bar = next(i for i, ln in enumerate(lines) if "░" in ln or "▓" in ln)
    window_pct = next(i for i, ln in enumerate(lines) if ln.startswith("prompt"))
    compaction = next(i for i, ln in enumerate(lines) if ln.startswith("compaction"))

    assert bar == window_pct + 1, (
        f"the bar is not under the window figure it draws: line {bar}, "
        f"window figure on line {window_pct}"
    )
    assert bar < compaction, (
        "the bar is still adjacent to the compaction estimate — the two figures "
        "this pane keeps separate read as one illustrating the other"
    )


def test_every_ctx_label_puts_its_value_in_the_same_column() -> None:
    """Tier 2: the Ctx labels are one column, not five that happen to be close.

    ``cache`` had drifted four characters short of its neighbours, so one line
    in six started somewhere else. Checked across every labelled line rather
    than against the one that was wrong — a width that is only asserted where it
    already broke cannot notice the next label added at the wrong one.
    """
    from reyn.interfaces.inline.textual_chat.chrome import ctx_pane_lines

    lines = ctx_pane_lines({"ctx_window": 128_000, "ctx_used": 54_000})
    labelled = [ln for ln in lines if ln[:1].strip()]
    assert labelled, "no labelled lines — this gate would be vacuous"
    starts = {
        next(i for i, ch in enumerate(ln) if i and ch != " " and ln[i - 1] == " ")
        for ln in labelled
    }
    first = min(starts)
    assert starts == {first}, (
        f"the value column starts in {sorted(starts)} — the labels are not one "
        f"column, they are several that happen to be close"
    )


#: The cost table's own lines (everything before the footnotes / token lines).
#: Scope names since #3691 — the table was transposed so they could be spelled
#: out instead of abbreviated to fit a value column.
_COST_TABLE_PREFIXES = ("COST", "Session", "Agent", "Project")


def _cost_table_rows(lines: "list[str]") -> "list[str]":
    return [ln for ln in lines if ln.startswith(_COST_TABLE_PREFIXES)]


def _value_column_ends(row: str) -> "tuple[int, ...]":
    """The end offsets of a table row's VALUE tokens (everything after the label).

    A geometry probe, not a format pin: it measures WHERE the columns land, and
    says nothing about how many spaces produced that. Cells are right-aligned, so
    two rows whose value tokens end at the same offsets are column-aligned; a row
    whose label is one char longer than the others shifts every one of its tokens
    and the tuples differ."""
    return tuple(m.end() for m in re.finditer(r"\S+", row))[1:]


@pytest.mark.asyncio
async def test_cost_table_value_columns_align_across_every_row(tmp_path) -> None:
    """Tier 2: every row of the cost table puts its value columns at the SAME
    offsets — a geometry invariant, not a rendered-string pin.

    ★ real-TTY-witnessed (tui-coder, #3341): the row labels used to be padded
    implicitly by their own literal width, and ``Output``/``Saved%`` are 6 chars
    while ``Total``/``Input``/``Saved`` are 5 — so those rows' cells sat one
    column left of the rest. The retired renderer had the identical defect and
    this surface inherited it in the port. The label column is now padded to the
    longest label (``chrome._COST_LABEL_W``), so adding or renaming a row later
    cannot re-break it.

    Deliberately exercised with a MIXED state set (Ses reconciles, Prj has no
    breakdown → ``—`` cells), since differing cell widths are exactly when a
    misalignment is easiest to miss by eye."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    snap["cost_breakdown_session"] = CostBreakdown(
        prompt_cost=0.0100, completion_cost=0.0100, cache_savings=0.0050
    )
    snap["cost_breakdown_agent"] = CostBreakdown(
        prompt_cost=1.0000, completion_cost=2.0000, cache_savings=0.5000
    )
    snap["cost_breakdown_project"] = CostBreakdown()
    snap["cost_usd"] = 0.0200
    snap["cost_agent"] = 3.0000
    snap["cost_total"] = 9.8765  # breakdown absent -> "unavail" column

    rows = _cost_table_rows(cost_pane_lines(snap))
    assert {row.split()[0] for row in rows} == set(_COST_TABLE_PREFIXES), (
        f"cost table lost or gained a row: {rows}"
    )
    offsets = {row: _value_column_ends(row) for row in rows}
    reference = offsets[rows[0]]
    misaligned = {
        row: ends for row, ends in offsets.items() if ends != reference
    }
    assert not misaligned, (
        f"these rows' value columns land elsewhere than the header's {reference}:\n"
        + "\n".join(f"{ends}  {row!r}" for row, ends in misaligned.items())
    )


@pytest.mark.asyncio
async def test_cost_table_stays_aligned_and_honest_past_the_1000_dollar_column_width(
    tmp_path,
) -> None:
    """Tier 2: #4544 — architect's own repro, at the real widths this table
    renders: every prior fixture in this file tops out at cost_total=9.8765
    (7 chars — fits _COST_COL_W=9 with room to spare), so the existing
    alignment test above is structurally incapable of exercising an
    over-width cell. This fixture pushes the Project scope's Total past
    $1000 (bug B's own threshold) with an ``approx`` state (bug A's own
    state), and checks TWO things the alignment-only test cannot: the value
    columns still land at the same offset (bug B), AND the displayed
    number is not a byte-truncated wrong value (bug A) — architect's own
    point that "the witness for bug A is the displayed amount, not just
    alignment", since a truncated ``~$999.999`` is 9 chars wide and would
    pass an alignment-only check."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    snap["cost_breakdown_session"] = CostBreakdown(
        prompt_cost=0.0100, completion_cost=0.0100, cache_savings=0.0050
    )
    snap["cost_breakdown_agent"] = CostBreakdown(
        prompt_cost=1.0000, completion_cost=2.0000, cache_savings=0.5000
    )
    # Components deliberately do NOT reconcile with the Total below -> "approx"
    # state (genuine >200k tiered pricing shape), the exact state bug A hit.
    snap["cost_breakdown_project"] = CostBreakdown(
        prompt_cost=800.0000, completion_cost=150.0000, cache_savings=40.0000
    )
    snap["cost_usd"] = 0.0200
    snap["cost_agent"] = 3.0000
    # architect's own repro table (#4544): 999.9999 at 4dp is exactly 9 chars
    # (fits without shedding — the Total column carries no '~' prefix, so it
    # has one more character of budget than an approx cell at the same
    # value). 12345.6789 genuinely needs precision shed even in the Total
    # column, so this exercises the exact-state rounding path too, not just
    # the approx one.
    snap["cost_total"] = 12345.6789

    rows = _cost_table_rows(cost_pane_lines(snap))
    project_row = next(row for row in rows if row.startswith("Project"))

    # Bug B: alignment must still hold at this width. Fixed-width slicing,
    # NOT the whitespace-run regex `_value_column_ends` above uses — a cell
    # that fills its full _COST_COL_W with no left-padding (exactly what a
    # correctly width-shed value does) abuts its neighbor with no space at
    # all, which would defeat a regex expecting a gap between tokens even
    # though the columns ARE correctly aligned (fixed character offsets).
    expected_len = _COST_LABEL_W + 5 * _COST_COL_W
    wrong_length = {row: len(row) for row in rows if len(row) != expected_len}
    assert not wrong_length, (
        f"these rows are not exactly {expected_len} chars (label + 5 fixed-width "
        f"columns), meaning some cell overflowed its column:\n"
        + "\n".join(f"{n} chars  {row!r}" for row, n in wrong_length.items())
    )

    # Bug A: the Total cell must be the correctly-rounded $12345.68, never a
    # byte-truncated "$12345.6" (a full digit short of the true value — this
    # is what the old ("~"+s)[:9]-style slicing would have produced had it
    # applied to the Total column too, which it never even attempted to).
    assert "$12345.68" in project_row, (
        f"Project row does not show the correctly-rounded total: {project_row!r}"
    )
    assert "$12345.6" not in project_row.replace("$12345.68", ""), (
        f"Project row shows a byte-truncated (wrong) value, not a rounding: "
        f"{project_row!r}"
    )
    # And the approx-state components must ALSO be correctly rounded, not
    # byte-sliced — the exact shape reyn-reviewer originally found.
    assert "~$800.000" in project_row
    assert "~$150.000" in project_row
    assert "~$40.0000" in project_row


@pytest.mark.asyncio
async def test_every_static_drawer_pane_shares_the_tabs_left_edge(tmp_path) -> None:
    """Tier 2: #4554 — the drawer's tab label sits 2 cells in from the left
    edge (MenuBar's own `padding: 0 1` + Tab's own `padding: 0 1`), but a
    Static pane's content used to start at column 0 with no shared left
    edge. Reads the REAL RESOLVED CSS via ``.styles.padding`` on a real
    mounted widget — not a string grep of the stylesheet source, which
    would only confirm the rule was EDITED, not that Textual actually
    applies it (a selector typo/specificity miss would still show the
    edited text in the source).

    Derives the pane-id set from `_MENU_TABS` minus `pane_is_list()`
    (the real registries #4554's own investigation used to enumerate all
    5 Static panes), rather than hand-listing cost/ctx/pipe/cron/help — a
    hand list would silently stop covering a 6th Static pane added later."""
    from textual.geometry import Spacing
    from textual.widgets import OptionList, Static

    from reyn.interfaces.inline.textual_chat import TextualChatApp
    from reyn.interfaces.inline.textual_chat.chrome import _MENU_TABS, pane_is_list

    static_pane_ids = [tab_id for tab_id, _label in _MENU_TABS if not pane_is_list(tab_id)]
    list_pane_ids = [tab_id for tab_id, _label in _MENU_TABS if pane_is_list(tab_id)]
    assert static_pane_ids and list_pane_ids, "pane registries are empty — fixture is stale"

    snap, _session, _registry = await _real_snapshot(tmp_path)
    transport = _EventOnlyTransport()
    app = TextualChatApp(transport=transport, read_model=_MutableSnapshotReadModel(snap))
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        wrong: dict[str, Spacing] = {}
        for tab_id in static_pane_ids:
            app._open_drawer(tab_id)
            await pilot.pause()
            padding = app.query_one(f"#{tab_id}", Static).styles.padding
            if padding != Spacing(top=1, right=2, bottom=1, left=2):
                wrong[tab_id] = padding
        assert not wrong, (
            f"these Static panes do not share the tab row's 2-cell left edge: {wrong}"
        )

        # OptionList panes must be UNCHANGED (architect's own exclusion,
        # #4554) — adding padding here would shrink the full-width row
        # highlight that selecting a row relies on.
        for tab_id in list_pane_ids:
            app._open_drawer(tab_id)
            await pilot.pause()
            padding = app.query_one(f"#{tab_id}", OptionList).styles.padding
            assert padding == Spacing(0, 0, 0, 0), (
                f"OptionList pane {tab_id!r} gained padding it must not have: {padding}"
            )


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

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


class _EventOnlyTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` fed one frame at a time. The
    liveness tests push ONLY ``EventFrame``s through it — never a DisplayFrame —
    so a status refresh that still sat on the DISPLAY leg can never be reached by
    the positive control."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()
        self.submitted: list[str] = []
        # #3595 S5: a menu row that dispatches a slash now RUNS it as a command
        # through this seam instead of submitting the line as a turn.
        self.commands: list[str] = []

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

    async def run_slash_command(self, name: str, args: str) -> bool:
        self.commands.append(f"/{name} {args}".rstrip())
        return True

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
@pytest.mark.parametrize("screen_size", [(100, 30), (80, 24), (60, 20)])
async def test_every_menu_tab_is_fully_on_screen(
    screen_size: "tuple[int, int]", tmp_path
) -> None:
    """Tier 2b: ★ real-TTY-witnessed geometry guard (#3338/#3341) — EVERY menu
    tab's region is fully inside the screen on BOTH axes (``x >= 0`` and
    ``x + width <= screen_width``; ``y >= 0`` and ``y + height <= screen_height``),
    at three widths.

    This is the CHILD plane. The existing #3311 containment gate
    (``test_pending_intervention_panel_does_not_swallow_the_screen``) measures
    parent widgets on the VERTICAL axis only — the menu row's own region is
    ``width: 100%`` and therefore always "contained", while the tabs INSIDE it
    were laid out past the right edge with that gate fully green. Same shape as
    the #3337 hole: a gate measuring the wrong plane.

    Measured against the pre-fix single-line ``Tabs`` row (13 tabs, one line):
    ``help`` at ``x=77 right=83`` on an 80-wide screen (1 tab off), and
    ``hook``/``cron``/``menu``/``help`` at ``right=65/71/77/83`` on a 60-wide
    screen (4 tabs off).

    BOTH axes are load-bearing, and the second one was found by falsifying this
    very test: with only the horizontal bounds asserted, pinning the wrapped row
    back to ``MenuBar { height: 1 }`` stayed GREEN — the overflow rows are then
    laid out at the correct x but BELOW the screen's last line, i.e. invisible
    again by a different route. A horizontal-only gate would have shipped that.
    The #3311 co-vet correction's lower-bound reasoning applies identically."""
    from textual.widgets import Tab

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    snap, _session, _registry = await _real_snapshot(tmp_path)
    app = TextualChatApp(
        transport=_EventOnlyTransport(), read_model=_MutableSnapshotReadModel(snap)
    )
    async with app.run_test(size=screen_size) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen.size
        tabs = list(app.query(Tab))
        assert {tab.id for tab in tabs} == {tid for tid, _label in _MENU_TABS}, (
            "wrapping the row dropped or duplicated a menu item: "
            f"{[tab.id for tab in tabs]}"
        )
        offenders = {
            tab.id: (tab.region.x, tab.region.right, tab.region.y, tab.region.bottom)
            for tab in tabs
            if tab.region.x < 0
            or tab.region.right > screen.width
            or tab.region.y < 0
            or tab.region.bottom > screen.height
        }
        assert not offenders, (
            f"tabs laid out off-screen at {screen_size} (screen={screen}); "
            f"offender -> (x, right, y, bottom): {offenders}"
        )


@pytest.mark.asyncio
async def test_telemetry_pins_to_the_right_edge_with_a_gap_from_navigation(
    tmp_path,
) -> None:
    """Tier 2b: #4542 — real-TTY-witnessed geometry guard for the stretching
    ``.menu-spacer`` between Navigation (tabs) and Telemetry (StatusLine),
    for the MERGED case specifically (StatusLine shares the last tab row —
    live-probed: this app's 14 tabs + status text all fit on one row
    starting at 130 columns, not 100 — an earlier version of this test
    picked 100 and, without checking, silently exercised the DIFFERENT
    own-row fallback path instead, where there is no spacer at all — see
    the sibling test below for that path's own, different mechanism).

    A screen wide enough to merge (130 columns) must show TWO things the
    spacer specifically causes, neither of which the pre-#4542
    immediately-adjacent layout produced: (1) StatusLine's region reaches
    (or comes very close to) the row's right edge — "pinned right", not
    merely "somewhere after the tabs"; (2) there is a real gap (more than
    the tabs' own inter-tab padding) between the last tab's right edge and
    StatusLine's left edge — proving an actual stretching space, not the
    two widgets sitting flush."""
    from textual.widgets import Tab

    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp

    snap, _session, _registry = await _real_snapshot(tmp_path)
    app = TextualChatApp(
        transport=_EventOnlyTransport(), read_model=_MutableSnapshotReadModel(snap)
    )
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen.size
        status = app.query_one(StatusLine)
        assert "-shared" in status.classes, (
            "this test requires the MERGED case (status sharing the last tab "
            "row) — got the own-row fallback instead; widen the screen size"
        )
        tabs = list(app.query(Tab))
        last_tab = max(tabs, key=lambda t: t.region.x)
        assert screen.width - status.region.right <= 1, (
            f"StatusLine not pinned to the right edge: right={status.region.right} "
            f"screen_width={screen.width}"
        )
        gap = status.region.x - last_tab.region.right
        assert gap > _TAB_H_PADDING, (
            f"no real gap between Navigation and Telemetry — the spacer did not "
            f"expand (gap={gap}, last_tab.right={last_tab.region.right}, "
            f"status.x={status.region.x})"
        )


@pytest.mark.asyncio
async def test_telemetry_own_row_stays_full_width_not_shrunk_by_a_spacer(
    tmp_path,
) -> None:
    """Tier 2b: #4542 — the own-row fallback (no merge — 100 columns, per
    the sibling test's own live-probed threshold) does NOT get a spacer:
    StatusLine keeps the base rule's ``width: 100%`` (load-bearing
    containment for a long status string — see app.py's ``StatusLine``
    CSS comment; an auto-width own-row status line can overflow past the
    screen edge) and is pinned right via ``text-align: right`` instead. A
    spacer here would have nothing to push against, since StatusLine
    already claims the whole row — this asserts that design choice
    directly rather than only the merged case above."""
    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp

    snap, _session, _registry = await _real_snapshot(tmp_path)
    app = TextualChatApp(
        transport=_EventOnlyTransport(), read_model=_MutableSnapshotReadModel(snap)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        status = app.query_one(StatusLine)
        assert "-shared" not in status.classes, (
            "this test requires the own-row fallback — got the merged case "
            "instead; narrow the screen size"
        )
        assert status.region.width >= status.parent.size.width - 2, (
            f"own-row StatusLine width shrank ({status.region.width}) — "
            f"containment protection for a long status string may be lost"
        )


def test_status_fits_last_row_pure() -> None:
    """Tier 1: :func:`status_fits_last_row` (#3326) — pure fit-decision helper.

    Mirrors the existing #3338 ``pack_menu_rows`` pure-function convention:
    testable without mounting anything."""
    from reyn.interfaces.inline.textual_chat.chrome import (
        pack_menu_rows,
        status_fits_last_row,
    )

    # All 13 (abbreviated) tabs pack onto one row at width=78+.
    rows = pack_menu_rows(_MENU_TABS, 100)
    # Plenty of room left (100 - 78 tabs = 22) for a short status string.
    assert status_fits_last_row(rows, 100, status_text_len=10) is True
    # Not enough room for a long one.
    assert status_fits_last_row(rows, 100, status_text_len=100) is False

    # A width forcing multiple rows: the LAST row (not the first, which is
    # nearly full) is what gets checked.
    narrow_rows = pack_menu_rows(_MENU_TABS, 40)
    last_row_used = sum(len(label) + 2 for _tid, label in narrow_rows[-1])
    max_fitting_len = 40 - last_row_used - 2  # 2 == _STATUS_H_PADDING
    assert status_fits_last_row(narrow_rows, 40, status_text_len=max_fitting_len) is True
    assert status_fits_last_row(narrow_rows, 40, status_text_len=max_fitting_len + 1) is False

    # No tabs at all: fits iff the text alone fits the width.
    assert status_fits_last_row([], 20, status_text_len=15) is True
    assert status_fits_last_row([], 20, status_text_len=25) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("screen_size", [(100, 30), (80, 24), (60, 20)])
async def test_status_line_on_screen_and_merge_matches_prediction(
    screen_size: "tuple[int, int]", tmp_path
) -> None:
    """Tier 2b: ★ real-TTY-witnessed geometry guard (#3326).

    Two invariants, both load-bearing:
    1. :class:`StatusLine` (the #2280 ONE always-visible chrome region — the
       halt banner rides on it) stays fully on screen on both axes, at every
       size, regardless of whether it merged onto a tab row or got its own.
    2. The live widget tree's ACTUAL merge decision (is StatusLine's parent
       row also carrying Tab children, or does it have a row alone?) matches
       what :func:`status_fits_last_row` predicts from the real packed rows
       and the real status text — i.e. the decision is genuinely reflected
       in what mounts, not merely computed and discarded."""
    from textual.widgets import Tab

    from reyn.interfaces.inline.textual_chat import TextualChatApp
    from reyn.interfaces.inline.textual_chat.chrome import (
        pack_menu_rows,
        status_fits_last_row,
    )

    snap, _session, _registry = await _real_snapshot(tmp_path)
    app = TextualChatApp(
        transport=_EventOnlyTransport(), read_model=_MutableSnapshotReadModel(snap)
    )
    async with app.run_test(size=screen_size) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen.size

        # query_one itself enforces "exactly one match" (raises otherwise) —
        # the uniqueness guarantee this test needs, with no separate len() check.
        line = app.query_one(StatusLine)
        assert line.region.x >= 0 and line.region.right <= screen.width, (
            f"StatusLine off-screen horizontally at {screen_size}: {line.region}"
        )
        assert line.region.y >= 0 and line.region.bottom <= screen.height, (
            f"StatusLine off-screen vertically at {screen_size}: {line.region}"
        )

        parent_row = line.parent
        tabs_sharing_the_row = list(parent_row.query(Tab)) if parent_row is not None else []
        actually_merged = bool(tabs_sharing_the_row)

        menubar = app.query_one(MenuBar)
        content_width = menubar.content_size.width or menubar.size.width
        rows = pack_menu_rows(_MENU_TABS, content_width)
        predicted_merge = status_fits_last_row(
            rows, content_width, len(status_line_text(snap, AGENT))
        )
        assert actually_merged == predicted_merge, (
            f"at {screen_size} (content_width={content_width}): predicted "
            f"merge={predicted_merge} but the live tree shows merged={actually_merged}"
        )


@pytest.mark.asyncio
async def test_status_line_stays_contained_with_a_long_raw_model_id(tmp_path) -> None:
    """Tier 2b: ★ real-TTY-witnessed geometry guard (#3326), dedicated
    long-string case.

    ``test_status_line_on_screen_and_merge_matches_prediction`` already goes
    RED on the ``width: auto`` overflow defect this guards against, but only
    incidentally — it depends on the real fixture snapshot's status text
    happening to be long enough at (60, 20). This test does not depend on
    that: it deliberately injects a long raw ``--model`` passthrough id (the
    #3324 shape — a model string matching no configured class), so the
    containment invariant is pinned independent of how long the default
    fixture's status text happens to be."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    long_raw_model = "some-provider/an-extremely-long-raw-model-identifier-98765"
    snap, _session, _registry = await _real_snapshot(tmp_path)
    # Shallow copy — the real snapshot carries live, unpicklable objects
    # (thread locks etc.) that deepcopy can't touch; only two keys change here.
    snap = {**snap, "model": long_raw_model, "model_active_class": None}
    app = TextualChatApp(
        transport=_EventOnlyTransport(), read_model=_MutableSnapshotReadModel(snap)
    )
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen.size
        line = app.query_one(StatusLine)
        assert long_raw_model in status_line_text(snap, AGENT), (
            "test setup did not actually produce a long status string"
        )
        assert line.region.x >= 0 and line.region.right <= screen.width, (
            f"StatusLine off-screen horizontally with a long raw model id: {line.region}"
        )
        assert line.region.y >= 0 and line.region.bottom <= screen.height, (
            f"StatusLine off-screen vertically with a long raw model id: {line.region}"
        )


@pytest.mark.asyncio
async def test_inline_code_style_toned_down_from_the_loud_default(tmp_path) -> None:
    """Tier 2: #3326 — rich.Markdown's default inline-code style ("bold cyan
    on black", ``rich.default_styles.DEFAULT_STYLES["markdown.code"]``) is
    overridden app-wide, not left at the loud default.

    Falsification: pre-fix, ``app.console.get_style("markdown.code")`` is
    exactly the bold+black-background default; this asserts it no longer is,
    and specifically that it carries neither ``bold`` nor a forced background
    (the two components that made it read as too strong)."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    snap, _session, _registry = await _real_snapshot(tmp_path)
    app = TextualChatApp(
        transport=_EventOnlyTransport(), read_model=_MutableSnapshotReadModel(snap)
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        style = app.console.get_style("markdown.code")
        assert not style.bold, f"inline-code style still bold: {style!r}"
        assert style.bgcolor is None, (
            f"inline-code style still forces a background: {style!r}"
        )
        # #3469: the push now carries the COMPLETE palette-derived family, not
        # just markdown.code — pin the APP-console seam for the leak the owner
        # review caught (H2/H3 resolving to rich's "underline magenta" / "bold
        # magenta" defaults). The full colour discipline is gated end-to-end in
        # test_markdown_palette_gate_3469.py; this asserts the same theme
        # actually reached THIS console.
        for heading in ("markdown.h2", "markdown.h3"):
            resolved = app.console.get_style(heading)
            assert resolved.color is None, (
                f"{heading} still carries a colour ({resolved.color!r}) on the "
                "app console — the palette markdown theme did not reach the "
                "Textual seam"
            )


@pytest.mark.asyncio
async def test_menubar_active_tab_is_not_toned_down_to_telemetry_dimness(
    tmp_path,
) -> None:
    """Tier 2: #4542 (owner ruling, REVERSES #3326's own relationship —
    see app.py's ``MenuBar Tab.-active`` rule for the full history). #3326
    matched the active tab's color to the status line's own muted tone so it
    would not read as a loud, distinct highlight; #4542's redesign explicitly
    wants the OPPOSITE — "選択中の項目のみ...強調表示を行う" (only the
    selected item gets emphasis) — and gives the status line (now Telemetry)
    its own dedicated, permanently-dim style instead. Keeping #3326's own
    test green under the new CSS would have meant asserting a relationship
    the owner deliberately reversed; this replaces it with the relationship
    #4542 actually specifies: the active tab's text-style includes ``bold``
    (emphasis) and stays UNDIMMED, while Telemetry's own style is ``dim``
    — the two segments read as distinctly different tones, active tab the
    brighter of the two, not matched to it."""
    from textual.widgets import Tab

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    snap, _session, _registry = await _real_snapshot(tmp_path)
    app = TextualChatApp(
        transport=_EventOnlyTransport(), read_model=_MutableSnapshotReadModel(snap)
    )
    async with app.run_test(size=(80, 24)) as pilot:
        # #3434: Textual applies the initial "-active" CSS class to a Tab
        # reactively (mount -> compose -> a Message that sets it), which is
        # not guaranteed to have settled after a single pump of the message
        # loop under load (observed: passes under a quiet `pytest`, fails
        # under `-n auto`'s worker-count CPU contention). The neighboring
        # test in this file (`test_status_line_long_raw_model_id_stays_
        # onscreen`) already double-pumps for the same reason; match it here.
        await pilot.pause()
        await pilot.pause()
        line = app.query_one(StatusLine)
        active_tab = next(tab for tab in app.query(Tab) if tab.has_class("-active"))
        assert active_tab.styles.text_style.bold, (
            f"active tab lost its bold emphasis: {active_tab.styles.text_style!r}"
        )
        assert not active_tab.styles.text_style.dim, (
            "active tab is toned down (dim) — #4542 reserves dim for "
            f"Telemetry only, active tab must stay undimmed: {active_tab.styles.text_style!r}"
        )
        assert line.styles.text_style.dim, (
            f"StatusLine (Telemetry) lost its dim style: {line.styles.text_style!r}"
        )


class _PaneRefreshCountingApp:
    """Mixin that RECORDS which panes the real app rebuilt, then delegates to the
    real implementation — an observer, never a substitute: ``_refresh_pane``'s
    actual body still runs, so what is counted is the production call."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.refreshed_panes: list[str] = []

    def _refresh_pane(self, tab_id, *args, **kwargs):  # type: ignore[override]
        # Pass every argument straight through — the observer must not alter the
        # call (defaulting ``snap`` here would silently turn an open-time refresh
        # into a pre-session "no snapshot" rebuild).
        self.refreshed_panes.append(tab_id)
        return super()._refresh_pane(tab_id, *args, **kwargs)  # type: ignore[misc]


class _CountingCompactionStatus:
    """A zero-arg callable that counts its invocations and DELEGATES to the real
    bound ``Session.context_window_status`` — the real expensive work still runs
    and the real dict flows into the pane. It occupies the exact slot
    ``_snapshot()`` fills with that same bound method, so nothing is faked; the
    counter just makes "was the expensive call made this frame?" observable."""

    def __init__(self, real_fn) -> None:
        self._real_fn = real_fn
        self.calls = 0

    def __call__(self) -> dict:
        self.calls += 1
        return self._real_fn()


@pytest.mark.asyncio
async def test_closed_pane_rebuild_is_not_invoked_on_frame_arrival(tmp_path) -> None:
    """Tier 2: the non-vacuity half of requirement 4(b) — when a frame lands, the
    rebuild for a pane that is NOT open is **not invoked at all**, counted on the
    real path.

    Asserting only "the closed pane's rendered content did not change" is
    satisfiable by a build that rebuilds every pane and happens to produce
    identical text — which is precisely the build this bound exists to forbid.
    What actually matters is the CALL: the Ctx pane resolves
    ``ctx_compaction_status_fn`` (= ``Session.context_window_status()``, a
    json.dumps + token estimate of the whole router-view history), and
    ``_snapshot()`` stores that method UNCALLED specifically so it never runs per
    frame. So this counts both planes — the pane rebuild, and the expensive call
    itself — and pairs the zero with a positive control (open Ctx, and the same
    frame DOES invoke it), so the zero cannot be passing because nothing happened
    at all."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    class _CountingApp(_PaneRefreshCountingApp, TextualChatApp):
        pass

    snap, session, _registry = await _real_snapshot(tmp_path)
    counting_status = _CountingCompactionStatus(session.context_window_status)
    snap["ctx_compaction_status_fn"] = counting_status

    transport = _EventOnlyTransport()
    app = _CountingApp(
        transport=transport, read_model=_MutableSnapshotReadModel(snap)
    )
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()

        # Cost open, Ctx closed. A frame lands.
        app._open_drawer("cost")
        await pilot.pause()
        app.refreshed_panes.clear()
        calls_before = counting_status.calls

        await transport.push_event(_llm_response_event())
        await pilot.pause()
        await pilot.pause()

        assert "cost" in app.refreshed_panes, (
            "the OPEN pane was not rebuilt on frame arrival — the positive "
            "control for this test did not fire"
        )
        assert "ctx" not in app.refreshed_panes, (
            f"a CLOSED pane's rebuild was invoked: {app.refreshed_panes}"
        )
        assert counting_status.calls == calls_before, (
            "the expensive compaction-status call ran for a pane that is not "
            f"open ({counting_status.calls - calls_before} extra calls) — the "
            "lazy seam has been demoted to per-frame evaluation"
        )

        # Positive control: with Ctx OPEN, the same frame DOES invoke it, so the
        # zero above is a real absence rather than a dead code path.
        app._open_drawer("ctx")
        await pilot.pause()
        app.refreshed_panes.clear()
        calls_before = counting_status.calls

        await transport.push_event(_llm_response_event())
        await pilot.pause()
        await pilot.pause()

        assert "ctx" in app.refreshed_panes, "the OPEN Ctx pane was not rebuilt"
        assert counting_status.calls > calls_before, (
            "the compaction status was never resolved even with Ctx open — the "
            "zero asserted above would be vacuous"
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
        assert f"/visibility {want} tool {target['name']}" in transport.commands, (
            f"the Tool row dispatched nothing: {transport.commands}"
        )
        assert app.query_one("#drawer", ContentSwitcher).display is False
