"""Tier 2: #5720 ② — a reactive (mid) spill's durable ``spill_record`` now
carries the REAL history turn it targeted, not a silently-defaulted ``1``.

architect's own naming of the defect: the fold callback
(``RouterLoopDriver._persist_recovery_fold``) already receives ``seq_by_id``
(``decompose_history_for_retry``'s own ``id(wire_dict) -> real seq`` map —
the ONLY way to recover a real seq from a wire dict, which structurally
carries none, ``SeqUnavailable.WIRE_DICTS_CARRY_NO_SEQ``). The spill
callback (``_spill_batch_for_retry``) sits in the SAME call block and did
not — its own ``_mid_seq_of`` fell back to ``turn.get("seq", 1)``, which
for a wire dict is ALWAYS the default. Provenance was never structurally
absent; it was one keyword away and unwired.

``seq`` itself (``MediaStore.save_tool_result``'s own kwarg) is NOT
repurposed — it is still the store's naming ordinal (head/tail's
``idx + 1`` is untouched, unaffected by this fix). For the mid path
specifically, that ordinal was ALWAYS meant to carry the turn's real seq
(mid has no positional convention the way head/tail do) — fixing
``_mid_seq_of`` to genuinely resolve it via ``seq_by_id`` corrects both
the store's own filename uniqueness for mid entries AND the pre-existing,
dedicated provenance field this record already carries
(``SPILL_TARGET_SEQ_META_KEY`` — a real field, not one this PR invents).

Real ``RouterLoopDriver`` + real ``RouterHistoryBuffer`` + real
``MediaStore``; the only stand-ins are the collaborators
``test_slash_model_router_integration.py``'s own real-driver test already
tolerates as fakes (``RouterHostAdapter``, budget tracker) — none of which
``_spill_batch_for_retry``'s own code path reads.
"""
from __future__ import annotations

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.chat_message import SPILL_TARGET_SEQ_META_KEY, ChatMessage
from reyn.runtime.services.budget_gateway import BudgetGateway
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer
from reyn.runtime.services.router_loop_driver import RouterLoopDriver
from tests._support.events import collect_events


class _FakeRouterHost:
    """Minimal stand-in — ``_spill_batch_for_retry``'s own code path never
    reads it (same tolerance ``test_slash_model_router_integration.py``'s
    own real-driver test already establishes for this exact collaborator)."""

    def _set_cancel_event(self, event):
        pass


class _FakeBudget(BudgetGateway):
    """#5748 (lead-coder finding, the 3rd instance of this same shape —
    #5734's ``_PickerReadModel``/``_TaskSnapshotReadModel`` were the
    first two): a hand-written double that only reimplements the methods
    THIS test drives goes stale the moment the real class gains a new
    one — ``RouterLoop`` construction now unconditionally reads
    ``self._budget.update_last_call_usage`` (#5745), which a bare
    ``_FakeBudget`` never had. Inheriting the real ``BudgetGateway``
    (cheap to construct — no I/O in ``__init__``) means every CURRENT
    and FUTURE method this test doesn't care about is the real,
    correct one by construction, never a manually-kept-in-sync copy."""

    def __init__(self) -> None:
        super().__init__(budget_tracker=None, events=EventLog(), agent_name="t-agent")

    def check_and_increment_router_cap(self, user_text):
        pass

    def extend_router_cap(self, additional):
        pass

    def add_router_usage(self, **kwargs):
        pass


class _FakeBudgetAdvisor:
    async def enforce_new_msg_budget(self, **kwargs):
        pass


async def _noop_limit_checkpoint(**kwargs):
    from types import SimpleNamespace
    return SimpleNamespace(allow_continue=True, extension=1)


def _make_real_driver(tmp_path, *, history: "list"):
    from reyn.config.chat import SafetyConfig
    from reyn.data.workspace.media_store import MediaStore
    store = MediaStore(
        project_root=tmp_path, agent_name="t-agent", session_id="t-session",
    )
    events = EventLog()
    compaction_cfg = CompactionConfig(use_chars4_estimate=True)
    history_buffer = RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=compaction_cfg,
        compaction_controller=None,
        model_fn=lambda: "test-model",
        events=events,
        media_store=store,
        router_host=None,
        universal_wrappers_enabled=False,
        non_interactive=True,
        history_appender=history.append,
    )
    driver = RouterLoopDriver(
        router_host=_FakeRouterHost(),
        safety=SafetyConfig(),
        router_max_iterations=1,
        budget_tracker=_FakeBudget(),
        non_interactive=True,
        exclude_tools=set(),
        budget=_FakeBudget(),
        resolver=None,
        compaction=compaction_cfg,
        compaction_controller=None,
        token_learner=None,
        events=events,
        model_override_fn=lambda: None,
        history_buffer=history_buffer,
        budget_advisor=_FakeBudgetAdvisor(),
        limit_checkpoint_fn=_noop_limit_checkpoint,
        next_seq_fn=lambda: 0,
        append_history_fn=history.append,
    )
    return driver, events


def _big_wire_turn(seq: int) -> dict:
    """A mid-face wire dict — role+content only, structurally NO ``seq``
    field (the exact shape ``decompose_history_for_retry`` hands
    ``retry_loop``, ``SeqUnavailable.WIRE_DICTS_CARRY_NO_SEQ``'s own
    subject) — ``seq`` here names the REAL history turn only via the
    ``seq_by_id`` map a caller supplies out of band, never via a key on
    this dict itself."""
    return {"role": "tool", "content": "Y" * 50_000, "spillability": "first_choice"}


def test_spill_record_carries_the_real_turn_seq_not_a_defaulted_one(tmp_path):
    """Tier 2: #5720 ② — spilling a mid candidate with a REAL, distinct
    seq_by_id entry produces a durable spill_record whose
    SPILL_TARGET_SEQ_META_KEY is that real seq — never the pre-#5720
    default of 1."""
    history: "list[ChatMessage]" = []
    driver, events = _make_real_driver(tmp_path, history=history)
    collect_events(events)

    turn = _big_wire_turn(seq=77)
    seq_by_id = {id(turn): 77}

    edits = driver._spill_batch_for_retry(
        [turn], chain_id="c1", seq_by_id=seq_by_id,
    )

    assert edits, "expected a real spill edit — the candidate is large and eligible"
    records = [m for m in history if m.role == "spill_record"]
    assert records, "expected a durable spill_record entry"
    assert records[-1].meta.get(SPILL_TARGET_SEQ_META_KEY) == 77, (
        f"expected the real turn seq (77), got "
        f"{records[-1].meta.get(SPILL_TARGET_SEQ_META_KEY)!r}"
    )


def test_spill_record_falls_back_to_1_only_when_provenance_is_genuinely_unavailable(
    tmp_path,
):
    """Tier 2: falsify pair (deny side) — when the candidate's own id is
    genuinely absent from seq_by_id (provenance truly could not be
    resolved, not merely unwired), the defensive fallback still produces
    a record rather than crashing — same shape the pre-#5720 code had,
    now reached only in the genuinely-unresolvable case instead of
    always."""
    history: "list[ChatMessage]" = []
    driver, events = _make_real_driver(tmp_path, history=history)
    collect_events(events)

    turn = _big_wire_turn(seq=77)
    empty_seq_by_id: "dict[int, int]" = {}

    edits = driver._spill_batch_for_retry(
        [turn], chain_id="c1", seq_by_id=empty_seq_by_id,
    )

    assert edits, "expected a real spill edit regardless of provenance resolution"
    records = [m for m in history if m.role == "spill_record"]
    assert records[-1].meta.get(SPILL_TARGET_SEQ_META_KEY) == 1
