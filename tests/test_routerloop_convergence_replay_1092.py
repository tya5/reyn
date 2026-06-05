"""Tier 3a: #1092 PR-C-2 — the CONVERGED op-loop is record/replay byte-identical.

lead-coder's C-2 gate: *record a converged op-loop fixture → replay byte-identical*.
This test is the EMPIRICAL proof — a real standard-model recording
(``gemini/gemini-2.5-flash-lite``), a committed fixture, replayed deterministically
in CI with no live LLM. A clean replay (every turn matches a recorded key, no
``MissingFixture``) demonstrates that the converged op-loop round-trips
deterministically, which is what justifies NOT adding tool_call-id normalization.

Empirical finding that decides the normalization question (#1092 PR-C-2)
------------------------------------------------------------------------
The recording revealed something stronger than the original flow-trace
hypothesis: the standard model does NOT emit native ``tool_calls`` at all — it
emits the act/decide envelope as TEXT-JSON ``content`` (``finish_reason="stop"``,
``tool_calls=None``). This is the documented Pattern-E envelope attractor (the
weak-model native-op-loop fumble, deferred to the post-unification chat-side fix —
out of scope here). The consequence for PR-C-2 is decisive: with the standard
model NO provider ``tool_call_id`` ever enters a replay key, so tool_call-id
normalization is *moot* — there are no non-deterministic ids to normalize. The
converged op-loop records and replays byte-identically as-is.

(Why it would still hold even WITH native tool_calls — flow-trace: within a
record→replay cycle the recorded assistant response carries the id, replay returns
that same recorded response, and the op-loop threads that id into the next turn's
keyed messages, so the next key matches. The id flows deterministically from the
recording. This test cannot capture that case empirically because the standard
model won't emit native tool_calls; strong models are out of policy.)

Fixture shape
-------------
The phase advertises ``read_file`` as a native tool (so the converged op-loop's
catalog/dispatch wiring is exercised) but directs an immediate finish, so the
recorded trajectory is a CLEAN 2-call converged run — op-loop turn (end_turn) then
the FD2 json-mode decide — rather than the weak-model decide-fumble (which, when it
occurs, *also* replays byte-identically but is a poor, behaviour-coupled fixture).

Deferred (tracked, NOT a silent gap): tool_call-id NORMALIZATION (rewriting
provider ids to deterministic sequential ids before keying) is for RE-RECORD
stability only — a *re-record* would draw fresh provider ids, shifting keys. It is
not a replay-correctness prerequisite and carries a blast radius (it would change
the keys of every existing native-tool fixture). YAGNI until a converged-fixture
re-record is operationally needed; trigger documented in the PR-C-2 body.

Recording
---------
First run (absent fixture) or ``REYN_LLM_RECORD=1`` records against the live
standard model; the committed fixture is then replayed deterministically in CI.

Tier 3a: one typical converged run, recorded then replayed.
"""
from __future__ import annotations

import asyncio
import datetime as _dt

import pytest

import reyn.schemas.models as _models
from reyn.agent import Agent
from reyn.config import ReynConfig
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.testing.replay import REPLAY_DATETIME

_SKILL_NAME = "converge_replay"
# Direct-to-Google standard model (GOOGLE_API_KEY), the free dogfood workhorse.
_MODEL = "gemini/gemini-2.5-flash-lite"


class _FrozenInstant(_dt.datetime):
    """A datetime whose ``astimezone`` is the identity — so the frame's
    ``current_datetime`` serialises to a fixed, tz-independent value on both
    record and replay (incl. CI in a different tz)."""

    def astimezone(self, tz=None):  # noqa: ANN001, ANN201
        return self


_FROZEN = _FrozenInstant(
    REPLAY_DATETIME.year, REPLAY_DATETIME.month, REPLAY_DATETIME.day,
    tzinfo=_dt.timezone.utc,
)


class _FrozenClock(_dt.datetime):
    """Drop-in for ``reyn.schemas.models.datetime`` so the ``ContextFrame``
    default factory (``datetime.now().astimezone()``) yields a fixed instant.
    Only ``now`` is overridden; all other datetime behaviour is inherited."""

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return _FROZEN


def _skill() -> Skill:
    # A single converged-op-loop phase. ``read_file`` is advertised as a native
    # tool so the op-loop's catalog/dispatch wiring is exercised, but the phase
    # directs an immediate finish (the standard model emits the envelope as
    # text-content, not native tool_calls — see module docstring).
    draft = Phase(
        name="draft",
        instructions=(
            "There is nothing to inspect and no file to read. Immediately finish "
            "this phase — do not call any tool."
        ),
        input_schema={"type": "object", "properties": {}},
        # Tools ARE advertised (so the converged op-loop's native-tool catalog /
        # dispatch wiring is exercised), but the phase directs an immediate finish
        # so the recorded trajectory is a clean success (a stable fixture), not the
        # weak-model decide-fumble (a deferred, out-of-scope concern for C-2).
        allowed_ops=["read_file"],
    )
    return Skill(
        name=_SKILL_NAME,
        entry_phase="draft",
        phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _event_kinds(sink: list) -> list[str]:
    out: list[str] = []
    for ev in sink:
        kind = getattr(ev, "type", None) or getattr(ev, "kind", None)
        if kind is None and isinstance(ev, dict):
            kind = ev.get("type") or ev.get("kind")
        if kind is not None:
            out.append(kind)
    return out


@pytest.mark.replay("fixtures/llm/routerloop_converged/phase_op_loop_byte_identical.jsonl")
def test_converged_op_loop_records_and_replays_deterministically(tmp_path, monkeypatch) -> None:
    """Tier 3a: a converged op-loop run records then replays deterministically.

    The committed fixture is replayed in CI; a clean replay (no MissingFixture,
    ``result.ok``) of this converged run proves the path round-trips
    deterministically — the PR-C-2 gate. Per the module docstring, the recorded
    standard model emits act/decide as text-content (Pattern E), so no provider
    tool_call_id enters a key and normalization is moot.
    """
    monkeypatch.chdir(tmp_path)
    # Freeze the frame's volatile ``current_datetime`` so the replay key (a hash
    # of the serialised messages) is identical across record and replay runs.
    monkeypatch.setattr(_models, "datetime", _FrozenClock)
    sink: list = []

    # Disable prompt caching: Gemini rejects caches below 2048 tokens, and this
    # minimal phase prompt is smaller; caching is also irrelevant to the replay
    # key (the key hashes messages, not cache directives).
    config = ReynConfig(
        tool_calls_op_loop_skills=[_SKILL_NAME], prompt_cache_enabled=False,
    )
    agent = Agent.from_config(
        config, model=_MODEL, subscribers=[sink.append],
    )
    result = asyncio.run(agent.run(_skill(), {"type": "input", "data": {}}))

    kinds = _event_kinds(sink)
    # BYTE-IDENTICAL gate: in replay mode (CI) a clean ``result.ok`` means every
    # turn of the multi-turn converged run matched a recorded fixture key with no
    # ``MissingFixture`` — i.e. the converged op-loop round-trips deterministically.
    # If any threaded id (or any message) had drifted, the key would miss here.
    assert result.ok, f"converged run must replay byte-identical; got {result.status}"
    # The CONVERGED path actually ran (not the #1212 frame-fed / json paths) — the
    # fixture is a converged-op-loop recording, the subject of the C-2 gate.
    assert "phase_routerloop_op_loop_started" in kinds, (
        "the recorded/replayed run must drive the converged op-loop"
    )
