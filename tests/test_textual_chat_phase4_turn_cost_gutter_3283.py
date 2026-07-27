"""Phase ④ remainder (#3283): per-turn cost/token in the right gutter.

#3337 landed the right gutter with ELAPSED TIME ONLY, because a per-turn
cost/token figure did not exist — per-call tokens/cost were folded straight into
cumulative counters with no turn key, and the only way to "recover" one later
would have been differencing cumulative counters, i.e. inventing a number.
#3339/#3342 fixed that at the SOURCE (``BudgetTracker``'s bounded per-turn
buckets, keyed by the turn's ``chain_id``). This completes ④ by rendering it.

#3339 deliberately CLOSED the keyed lookup: with no consumer, nothing would
enforce branching on "unknown", so the API was narrowed to
``latest_turn_usage()`` to make the ambiguous question unaskable. The gutter is
that consumer, so the lookup is reintroduced WITH the contract — ``None`` for a
turn the runtime holds no figure for — and the gates below are what enforce the
branching that #3339 had nothing to enforce.

These pin:

- **the keyed lookup's three outcomes, asserted as DISTINCT** (Tier 2, real
  ``BudgetTracker``): a recorded turn yields its real summed figures; a turn
  EVICTED past ``TURN_BUCKET_CAP`` yields ``None``; a ``chain_id`` never seen
  yields ``None``. Never a ``0`` for either unknown case.
- **the same three outcomes on the RENDERED gutter cell** (Tier 1): the figure,
  ``—``, ``—`` — read off ``decorate()``'s output, and asserted mutually
  distinct in one place so "all three render the same thing" cannot pass.
- **no non-zero spend ever renders as free** (Tier 1): a sub-hundredth-of-a-cent
  turn renders a ``<``-prefixed floor, never ``$0`` (which is reserved for a
  genuinely zero — unpriced-model — turn) and never ``—``.
- **the anchor decision** (Tier 1): the turn figure rides the ``kind="agent"``
  reply row that concludes a turn's visible output, NOT every row of the turn —
  the same total repeated N times in a column whose other label family is
  per-row would read as N separate per-row costs.
- **restore renders nothing rather than something reconstructed** (Tier 1, at
  the SOURCE as well as the gutter): ``project_restored_frames`` does not carry
  ``chain_id`` onto a restored frame, and the per-turn buckets are in-memory
  live-session state a restart does not rehydrate — so a restored conversation
  shows no cost figures at all. Same posture #3337 landed for elapsed.
- **end-to-end through the mounted app** (Tier 2b): a real ``TextualChatApp``
  whose read model hands over a REAL ``BudgetTracker``'s bound
  ``turn_usage`` renders the priced turn's figure and the unknown turn's ``—``
  on the composed row text, not just in a unit-level ``decorate()`` call.
- **the #3337 body-width floor still holds at the widened gutter** (Tier 2b):
  re-asserted here against the SHIPPED :data:`RIGHT_GUTTER_WIDTH` so widening
  it in future is caught by this file too, not only by #3337's own gate.

All collaborators are real: a real :class:`BudgetTracker` (its bound
``turn_usage`` IS the production lookup), a real ``FlowModel``/``Entry``, real
``OutboxMessage``, a real mounted ``TextualChatApp``, and a real
:class:`~reyn.interfaces.repl.read_model.ChatReadModel` seam implementation
(the same shape ``RegistryReadModel``/``RemoteReadModel`` have) — no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual_flowview import FlowModel, FlowView

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import (
    ReynRightGutter,
    ReynTimingGutter,
    ReynTurnUsageGutter,
    TextualChatApp,
)
from reyn.interfaces.inline.textual_chat._meta_keys import RUNNING_SINCE_KEY
from reyn.interfaces.inline.textual_chat.gutter import (
    RIGHT_GUTTER_WIDTH,
    TURN_ANCHOR_KIND,
    TURN_USAGE_UNKNOWN,
)
from reyn.interfaces.inline.textual_chat.restore import project_restored_frames
from reyn.interfaces.repl.read_model import ChatReadModel, project_remote_snapshot
from reyn.interfaces.repl.status import _snapshot
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import TURN_BUCKET_CAP, BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session

_MODEL = "gpt-4o"
_LEFT_GUTTER_WIDTH = 2


def _agent_row(chain_id: "str | None") -> OutboxMessage:
    """An agent-reply display frame — the turn-figure ANCHOR row. ``chain_id``
    is the same key ``RouterLoop`` stamps on the real frame's meta."""
    meta = {"chain_id": chain_id} if chain_id is not None else {}
    return OutboxMessage(kind=TURN_ANCHOR_KIND, text="done", meta=meta)


def _entry(item: OutboxMessage):
    model: "FlowModel[OutboxMessage]" = FlowModel()
    return model.append(item)


def _label(gutter, item: OutboxMessage) -> str:
    """The rendered right-gutter cell for ``item``, stripped of alignment
    padding — read off ``decorate()``, never off a source meta field."""
    return gutter.decorate(_entry(item), RIGHT_GUTTER_WIDTH, 1).plain.strip()


def _record(tracker: BudgetTracker, chain_id: str, prompt: int, completion: int) -> None:
    tracker.record_llm(
        model=_MODEL,
        agent="alpha",
        usage=TokenUsage(prompt_tokens=prompt, completion_tokens=completion),
        chain_id=chain_id,
    )


# ── Gate 1: the keyed lookup's three outcomes (Tier 2, real BudgetTracker) ────


def test_keyed_turn_usage_answers_known_evicted_and_unseen_distinctly() -> None:
    """Tier 2: ``BudgetTracker.turn_usage(chain_id)`` — the lookup #3339
    deliberately closed, reintroduced WITH its contract. Three outcomes, and
    the point of asserting them together is that they must not collapse into
    each other: a recorded turn gets its real summed figures, while an EVICTED
    turn and a NEVER-SEEN chain_id both get ``None`` — never a fabricated 0,
    which would be indistinguishable from a genuinely free turn."""
    tracker = BudgetTracker(CostConfig())

    # Fill past the cap so the earliest turns are genuinely evicted, each with a
    # distinct total so an eviction cannot be masked by equality.
    turns = [f"turn-{i:03d}" for i in range(TURN_BUCKET_CAP + 2)]
    for i, chain_id in enumerate(turns):
        _record(tracker, chain_id, prompt=100 + i, completion=7)

    evicted, kept = turns[0], turns[-1]

    known = tracker.turn_usage(kept)
    assert known is not None, "a turn still in the buckets must have a figure"
    assert known["chain_id"] == kept
    assert known["tokens"] == 100 + len(turns) - 1 + 7, (
        "the figure must be the real sum of that turn's own calls"
    )
    assert known["cost_usd"] > 0.0, (
        f"{_MODEL} is priced, so a real turn's cost must be non-zero — "
        "otherwise the cost half of this gate is vacuous"
    )

    assert tracker.turn_usage(evicted) is None, (
        "an evicted turn must read as UNKNOWN, never as a 0 total"
    )
    assert tracker.turn_usage("turn-never-seen") is None, (
        "a chain_id this tracker never saw must read as UNKNOWN"
    )
    # ...and the three are not the same answer wearing different hats.
    assert known != tracker.turn_usage(evicted)


def test_keyed_turn_usage_sums_a_multi_call_turn_and_isolates_turns() -> None:
    """Tier 2: a turn's figure is the sum over EVERY LLM call it made (a
    tool-loop turn makes several), and one turn's spend never leaks into
    another's — the property that makes a per-row figure meaningful at all."""
    tracker = BudgetTracker(CostConfig())
    _record(tracker, "turn-A", prompt=1234, completion=567)
    _record(tracker, "turn-A", prompt=89, completion=21)
    _record(tracker, "turn-B", prompt=4321, completion=765)

    a = tracker.turn_usage("turn-A")
    b = tracker.turn_usage("turn-B")
    assert a is not None and b is not None
    assert a["tokens"] == 1234 + 567 + 89 + 21
    assert b["tokens"] == 4321 + 765
    assert a["cost_usd"] > b["cost_usd"] or a["tokens"] != b["tokens"], (
        "the two turns must be separately accounted"
    )


def test_a_turnless_call_creates_no_lookup_answer() -> None:
    """Tier 2: negative control — a call made outside any turn is still
    recorded cumulatively, but it neither creates a bucket of its own nor
    joins another turn's, so no keyed lookup can surface it."""
    tracker = BudgetTracker(CostConfig())
    _record(tracker, "turn-A", prompt=100, completion=10)
    before = tracker.turn_usage("turn-A")

    tracker.record_llm(
        model=_MODEL,
        agent="alpha",
        usage=TokenUsage(prompt_tokens=777, completion_tokens=333),
    )

    assert tracker.agent_tokens("alpha") == 110 + 1110, (
        "the turnless call must still be RECORDED, or this control is vacuous"
    )
    assert tracker.turn_usage("turn-A") == before, (
        "a turnless call must not be folded into any turn's figure"
    )


# ── Gate 2: the same three outcomes on the RENDERED cell (Tier 1) ─────────────


def test_gutter_renders_figure_for_known_turn_and_dash_for_unknown_and_evicted() -> None:
    """Tier 1: the three outcomes as the operator sees them, asserted MUTUALLY
    DISTINCT so "every row renders the same thing" cannot pass. A priced turn
    shows tokens + USD; an EVICTED turn and an UNSEEN chain_id both show
    :data:`TURN_USAGE_UNKNOWN` — never ``0``, and never an empty cell (which on
    a row that names a turn would read as "this turn was free")."""
    tracker = BudgetTracker(CostConfig())
    turns = [f"turn-{i:03d}" for i in range(TURN_BUCKET_CAP + 2)]
    for i, chain_id in enumerate(turns):
        _record(tracker, chain_id, prompt=1000 + i, completion=200)
    evicted, kept = turns[0], turns[-1]

    gutter = ReynTurnUsageGutter(usage_lookup=tracker.turn_usage)
    known_label = _label(gutter, _agent_row(kept))
    evicted_label = _label(gutter, _agent_row(evicted))
    unseen_label = _label(gutter, _agent_row("turn-never-seen"))

    assert known_label not in ("", TURN_USAGE_UNKNOWN), known_label
    assert "$" in known_label, f"the USD half is missing: {known_label!r}"
    assert "0" not in known_label.split()[0] or known_label.split()[0] != "0", known_label
    assert evicted_label == TURN_USAGE_UNKNOWN, evicted_label
    assert unseen_label == TURN_USAGE_UNKNOWN, unseen_label
    assert known_label != evicted_label, (
        "a known turn and an evicted one must not render identically"
    )


def test_a_raising_lookup_degrades_to_unknown_rather_than_killing_the_render() -> None:
    """Tier 1: the error path. ``decorate`` runs on every gutter repaint, so a
    lookup that raises must not be able to take the whole render down — it
    degrades to :data:`TURN_USAGE_UNKNOWN`, the same honest answer as any other
    "no figure" case, and certainly not to a fabricated 0."""

    def _boom(chain_id: str) -> "dict | None":
        raise RuntimeError("tracker exploded")

    gutter = ReynTurnUsageGutter(usage_lookup=_boom)
    assert _label(gutter, _agent_row("turn-A")) == TURN_USAGE_UNKNOWN


def test_no_lookup_wired_still_renders_unknown_not_a_zero() -> None:
    """Tier 1: with no lookup available at all (no read model yet, or a REMOTE
    client — the per-turn buckets are session-local and not on the wire), a row
    that NAMES a turn still renders ``—``. The failure mode being closed is a
    silently blank cell, which reads as a free turn."""
    gutter = ReynTurnUsageGutter(usage_lookup=None)
    assert _label(gutter, _agent_row("turn-somewhere")) == TURN_USAGE_UNKNOWN


def test_remote_snapshot_publishes_no_turn_usage_lookup() -> None:
    """Tier 1: pins the remote leg at its SOURCE rather than only at the
    gutter's ``—`` — ``project_remote_snapshot`` hands over ``None`` for the
    keyed lookup, because per-turn buckets are session-local and are not
    projected onto the AG-UI wire (the same frame-sufficiency boundary as the
    past-turn conversation log)."""
    assert project_remote_snapshot({"model": "gpt-4o"})["turn_usage_fn"] is None


def test_zero_cost_turn_renders_a_real_zero_distinct_from_unknown() -> None:
    """Tier 1: ``$0`` and ``—`` mean different things and must render
    differently. A turn recorded against an UNPRICED model genuinely cost
    nothing — that is a real figure, and it must not be laundered into
    "unknown"; conversely "unknown" must not be laundered into ``$0``."""
    tracker = BudgetTracker(CostConfig())
    tracker.record_llm(
        model="a-model-no-catalog-prices",
        agent="alpha",
        usage=TokenUsage(prompt_tokens=500, completion_tokens=100),
        chain_id="turn-free",
    )
    usage = tracker.turn_usage("turn-free")
    assert usage is not None and usage["cost_usd"] == 0.0, (
        "fixture must actually be an unpriced (zero-cost) turn"
    )

    gutter = ReynTurnUsageGutter(usage_lookup=tracker.turn_usage)
    label = _label(gutter, _agent_row("turn-free"))
    assert label != TURN_USAGE_UNKNOWN, (
        f"a genuinely free turn must show its real $0, not 'unknown': {label!r}"
    )
    assert "600" in label, f"the token half must still be the real count: {label!r}"
    assert "$0" in label, label


def test_a_tiny_but_real_spend_never_renders_as_free() -> None:
    """Tier 1: the rounding hazard — a turn whose cost is smaller than the
    smallest exactly-printable figure must NOT render as ``$0`` (free) or as
    ``—`` (unknown). It renders a ``<``-prefixed floor: real spend, honestly
    bounded."""
    gutter = ReynTurnUsageGutter(
        usage_lookup=lambda cid: {"chain_id": cid, "tokens": 3, "cost_usd": 1e-9}
    )
    label = _label(gutter, _agent_row("turn-tiny"))
    assert label != TURN_USAGE_UNKNOWN, label
    assert "$0 " not in label and not label.endswith("$0"), (
        f"a real non-zero spend rendered as free: {label!r}"
    )
    assert "<" in label, f"expected a bounded floor form, got {label!r}"


# ── Gate 3: the anchor decision — one figure per turn, not one per row ────────


@pytest.mark.parametrize(
    "item",
    [
        OutboxMessage(kind="user", text="hi", meta={"chain_id": "turn-A"}),
        OutboxMessage(
            kind="tool_call_started",
            text="grep",
            meta={"chain_id": "turn-A", "op_id": "op-1"},
        ),
        OutboxMessage(kind="reasoning", text="thinking", meta={"chain_id": "turn-A"}),
    ],
)
def test_non_anchor_rows_of_a_priced_turn_show_no_cost_figure(
    item: OutboxMessage,
) -> None:
    """Tier 1: the turn figure is ANCHORED to the reply row, so the turn's OTHER
    rows — its user line, its tool calls, its reasoning — render no cost cell
    even though their frames carry the very same priced ``chain_id``. Repeating
    one turn total down every row of the turn, in a column whose other label
    family (elapsed) IS per-row, would read as N separate per-row costs."""
    tracker = BudgetTracker(CostConfig())
    _record(tracker, "turn-A", prompt=1000, completion=200)
    gutter = ReynTurnUsageGutter(usage_lookup=tracker.turn_usage)

    assert tracker.turn_usage("turn-A") is not None, (
        "fixture must price turn-A, or this negative control is vacuous"
    )
    assert _label(gutter, item) == "", (
        f"a non-anchor row of a priced turn must show no cost figure: {item.kind}"
    )
    # ...while the anchor row of that SAME turn does — so the empty cells above
    # are the anchoring decision, not a broken lookup.
    assert _label(gutter, _agent_row("turn-A")) != ""


def test_a_row_naming_no_turn_renders_an_empty_cell_not_a_dash() -> None:
    """Tier 1: ``""`` and ``—`` are also different statements. A row that names
    NO turn has no turn to report on — nothing is *unknown* about it — so it
    renders an empty cell, exactly as #3337's elapsed negative control does.
    ``—`` is reserved for "we know which turn, we do not know the figure"."""
    gutter = ReynTurnUsageGutter(usage_lookup=lambda cid: None)
    assert _label(gutter, _agent_row(None)) == ""
    assert _label(gutter, OutboxMessage(kind="agent", text="hi", meta={})) == ""


# ── Gate 4: restore — nothing reconstructed, at the source and at the gutter ──


def test_restore_projection_never_carries_a_turn_key() -> None:
    """Tier 1: pins the live-vs-restore split at the SOURCE, not only at the
    gutter's blank render — ``project_restored_frames`` stamps no ``chain_id``
    on any projected frame, so a restored conversation cannot even name a turn
    to price. (The second, independent reason it renders nothing: the per-turn
    buckets are in-memory live-session state a restart does not rehydrate.)"""
    log = [
        ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="hi there"),
    ]
    frames = project_restored_frames(log)
    assert frames, "fixture must project at least one frame"
    for frame in frames:
        assert "chain_id" not in (frame.meta or {}), (
            f"a restored {frame.kind} frame named a turn: {frame.meta!r}"
        )


def test_a_restored_reply_row_renders_no_cost_even_with_a_live_tracker() -> None:
    """Tier 1: the restore leg at the GUTTER, driven with a real tracker that
    DOES hold figures — a restored reply row still shows nothing, because the
    projected frame names no turn. Rules out "it looked blank only because the
    tracker was empty"."""
    tracker = BudgetTracker(CostConfig())
    _record(tracker, "turn-A", prompt=1000, completion=200)
    gutter = ReynTurnUsageGutter(usage_lookup=tracker.turn_usage)

    frames = project_restored_frames([ChatMessage(role="assistant", content="hi")])
    replies = [f for f in frames if f.kind == TURN_ANCHOR_KIND]
    assert replies, "fixture must project an agent reply frame"
    for frame in replies:
        assert _label(gutter, frame) == ""


# ── Gate 5: the composite column carries BOTH label families ─────────────────


def test_the_composite_right_gutter_carries_both_label_families() -> None:
    """Tier 1: :class:`ReynRightGutter` — the decorator actually wired into
    ``FlowView(right_decorator=…)`` — emits the ELAPSED label on a running tool
    row and the TURN-COST label on the reply row, through the SAME column. Both
    halves are load-bearing: a composite that dropped either would fail here
    while each half's own unit test still passed."""
    tracker = BudgetTracker(CostConfig())
    _record(tracker, "turn-A", prompt=1000, completion=200)
    gutter = ReynRightGutter(clock=lambda: 107.0, usage_lookup=tracker.turn_usage)

    running = OutboxMessage(
        kind="tool_call_started",
        text="grep",
        meta={"op_id": "op-1", "chain_id": "turn-A", RUNNING_SINCE_KEY: 100.0},
    )
    assert _label(gutter, running) == "7s", (
        "the elapsed half must still speak through the composite"
    )
    reply = _label(gutter, _agent_row("turn-A"))
    assert "$" in reply, f"the turn-cost half is missing: {reply!r}"


def test_every_label_the_column_can_emit_fits_the_configured_width() -> None:
    """Tier 1: :data:`RIGHT_GUTTER_WIDTH` is COMPUTED from the widest label,
    not guessed — so an extreme-but-reachable figure must not overflow the
    column. Drives the widest shapes of both halves (a huge token count, a
    huge cost, the sub-floor cost form, a long elapsed) through the real
    composite."""
    token_edges = [
        0, 1, 999, 1_000, 9_949, 9_950, 9_999, 999_499, 999_500,
        9_949_999, 9_950_000, 987_654_321,
    ]
    cost_edges = [
        0.0, 1e-9, 0.0001, 0.00999, 0.01, 9.999, 99.994, 99.999, 100.0, 98765.4321,
    ]
    for tokens in token_edges:
        for cost_usd in cost_edges:
            usage = {"chain_id": "t", "tokens": tokens, "cost_usd": cost_usd}
            gutter = ReynRightGutter(usage_lookup=lambda cid, u=usage: u)
            label = _label(gutter, _agent_row("t"))
            assert len(label) <= RIGHT_GUTTER_WIDTH - 1, (
                f"{label!r} ({len(label)} cols) leaves no breathing room in "
                f"RIGHT_GUTTER_WIDTH={RIGHT_GUTTER_WIDTH} "
                f"(tokens={tokens}, cost_usd={cost_usd})"
            )
    elapsed = ReynTimingGutter(clock=lambda: 10_000_000.0)
    long_elapsed = _label(
        elapsed,
        OutboxMessage(
            kind="tool_call_started", text="t", meta={RUNNING_SINCE_KEY: 0.0}
        ),
    )
    assert 0 < len(long_elapsed) <= RIGHT_GUTTER_WIDTH, long_elapsed


# ── Gate 6: the real status snapshot actually publishes the lookup ────────────


@pytest.mark.asyncio
async def test_the_real_status_snapshot_publishes_a_working_keyed_lookup(
    tmp_path,
) -> None:
    """Tier 2: the PUBLICATION path, through production code only — a real
    ``AgentRegistry`` + attached ``Session`` + ``BudgetTracker``, and the real
    ``interfaces/repl/status.py`` ``_snapshot()``. Its ``turn_usage_fn`` must be
    a live keyed lookup (``Session.turn_usage`` → ``BudgetTracker.turn_usage``),
    not merely a present key: the recorded turn's real figures come back and an
    unseen chain_id comes back ``None``.

    Without this, the app-level gates below would still pass while the snapshot
    published nothing at all — they supply their own read model."""

    agent = "gutter-cost-3283-agent"
    tracker = BudgetTracker(CostConfig())
    state_log = StateLog(tmp_path / "state.wal")
    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda profile: make_session(
            agent_name=profile.name,
            state_log=state_log,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
            budget_tracker=tracker,
        ),
        state_log=state_log,
    )
    AgentProfile.new(agent, role="").save(tmp_path / ".reyn" / "agents" / agent)
    await registry.attach(agent)

    _record(tracker, "turn-published", prompt=2000, completion=400)

    snap = _snapshot(registry)
    assert snap is not None, "the real producer returned no snapshot"
    lookup = snap["turn_usage_fn"]
    assert lookup is not None, "the real snapshot published no keyed lookup"

    usage = lookup("turn-published")
    assert usage is not None and usage["tokens"] == 2400, (
        f"the published lookup is not live: {usage!r}"
    )
    assert lookup("turn-never-happened") is None, (
        "the published lookup must answer UNKNOWN for a turn it has no figure "
        "for — never a fabricated 0"
    )


# ── Gate 7: end-to-end through the mounted app ────────────────────────────────


class _TurnUsageReadModel(ChatReadModel):
    """A real :class:`ChatReadModel` seam implementation (same shape as
    ``RegistryReadModel`` / ``RemoteReadModel``) whose snapshot hands over a
    REAL :class:`BudgetTracker`'s bound ``turn_usage`` — the very callable
    ``status.py``'s ``_snapshot()`` publishes as ``turn_usage_fn`` in
    production. Nothing about the lookup is simulated; only the surrounding
    session/registry is skipped."""

    def __init__(self, tracker: BudgetTracker) -> None:
        self._tracker = tracker

    def snapshot(self, config=None):
        return {"turn_usage_fn": self._tracker.turn_usage}

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
        return Path("/tmp/reyn_3283_p4_cost_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        return []


class _QueueTransport(ClientTransport):
    """A real :class:`ClientTransport` fed one frame at a time from a queue, so
    a test can push frames and inspect the render in between with the stream
    staying open (the helper shape #3337's gutter tests use)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[OutboxMessage]" = asyncio.Queue()
        self.submitted: list[str] = []

    async def push(self, msg: OutboxMessage) -> None:
        await self._queue.put(msg)

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        while True:
            yield DisplayFrame(await self._queue.get())

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

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _rendered_lines(flow: "FlowView[OutboxMessage]") -> "list[str]":
    """Every composed row line (left gutter + body + right gutter) of the
    FlowView's virtual content, via flowview's own selection extraction."""
    from textual.geometry import Offset
    from textual.selection import Selection

    height = max(0, flow.virtual_size.height - 1)
    result = flow.get_selection(
        Selection(Offset(0, 0), Offset(flow.virtual_size.width, height))
    )
    assert result is not None
    return [ln for ln in result[0].split("\n") if ln.strip()]


@pytest.mark.asyncio
async def test_mounted_app_prices_a_known_turn_and_dashes_an_unknown_one() -> None:
    """Tier 2b: end-to-end through the REAL mounted app — the real read-model
    seam, the real ``turn_usage_fn`` publication path, the real
    ``right_decorator`` wiring — a reply row for a PRICED turn ends in its real
    figure and a reply row for an unknown turn ends in ``—``, read off the
    COMPOSED row text rather than a ``decorate()`` call. Both rows are in the
    same render, so a wiring that fell back to one answer for everything fails
    on the other row."""
    tracker = BudgetTracker(CostConfig())
    _record(tracker, "turn-priced", prompt=1500, completion=250)

    transport = _QueueTransport()
    app = TextualChatApp(
        transport=transport, read_model=_TurnUsageReadModel(tracker)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push(
            OutboxMessage(
                kind="agent", text="priced reply", meta={"chain_id": "turn-priced"}
            )
        )
        await transport.push(
            OutboxMessage(
                kind="agent", text="mystery reply", meta={"chain_id": "turn-gone"}
            )
        )
        await pilot.pause()
        await pilot.pause()

        lines = _rendered_lines(app.query_one(FlowView))
        priced = [ln for ln in lines if "priced reply" in ln]
        mystery = [ln for ln in lines if "mystery reply" in ln]
        assert priced and mystery, lines

        assert any("$" in ln for ln in priced), (
            f"the priced turn's row carries no USD figure: {priced!r}"
        )
        assert not any(ln.rstrip().endswith(TURN_USAGE_UNKNOWN) for ln in priced), (
            f"a priced turn rendered as unknown: {priced!r}"
        )
        assert any(ln.rstrip().endswith(TURN_USAGE_UNKNOWN) for ln in mystery), (
            f"an unpriced turn did not render {TURN_USAGE_UNKNOWN!r}: {mystery!r}"
        )
        assert not any("$" in ln for ln in mystery), (
            f"an unknown turn was given a USD figure: {mystery!r}"
        )


@pytest.mark.asyncio
async def test_widened_gutter_still_leaves_the_body_most_of_the_terminal() -> None:
    """Tier 2b: ★ the #3337 body-width floor, re-asserted against the width
    this PR ships. Adding the cost/token label widened
    :data:`RIGHT_GUTTER_WIDTH`, and #3337's gate exists precisely because the
    file's other gates impose no upper bound on the gutters' share. Measured on
    the BODY width ``ReynPresenter.present`` actually receives — the only
    surface that exposes gutter consumption (``FlowView.region.width`` stays
    the full terminal width regardless).

    Asserted as the ARITHMETIC the width constant claims (screen − left gutter
    − right gutter), so a future widening that still cleared the half-screen
    floor but stopped matching the documented computation is also caught."""
    from reyn.interfaces.inline.textual_chat.presenter import ReynPresenter

    class _WidthRecordingPresenter(ReynPresenter):
        def __init__(self) -> None:
            super().__init__()
            self.widths: "list[int]" = []

        async def present(self, item: "OutboxMessage", width: int):
            self.widths.append(width)
            return await super().present(item, width)

    transport = _QueueTransport()
    presenter = _WidthRecordingPresenter()
    app = TextualChatApp(transport=transport, presenter=presenter)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await transport.push(OutboxMessage(kind="agent", text="a reply"))
        await pilot.pause()

        assert presenter.widths, "presenter never received a body width to record"
        body_width = presenter.widths[-1]
        screen = app.size.width
        assert body_width >= screen // 2, (
            f"the gutters left only {body_width} body columns out of {screen} — "
            f"RIGHT_GUTTER_WIDTH={RIGHT_GUTTER_WIDTH} breaks the #3337 floor"
        )
        assert body_width == screen - _LEFT_GUTTER_WIDTH - RIGHT_GUTTER_WIDTH, (
            f"body width {body_width} does not match the documented computation "
            f"{screen} - {_LEFT_GUTTER_WIDTH} - {RIGHT_GUTTER_WIDTH}"
        )
