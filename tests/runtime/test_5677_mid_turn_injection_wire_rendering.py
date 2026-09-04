"""Tier 2: #5677 §0 (architect co-vet, the heaviest requirement on the whole
issue) — mid-turn-injected content renders on the WIRE (and, byte-identically,
in HISTORY) under its OWN ``TurnOrigin``, never hardcoded to ``role="user"``.

Root cause this closes: widening ``MID_TURN_INJECTABLE`` past ``CLIENT_INPUT``
(#5677's own change) without ALSO widening the rendering would make a peer's
injected text indistinguishable from the operator's own typed line, mid-turn
— reproducing #3595's own closed defect class (a non-human producer claiming
the operator's voice) one layer down, on the mid-turn wire instead of the
inbox kind.

Real ``Session`` throughout (the same convention
``tests/core/test_3792_pr2_session_injection.py`` uses) — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import _render_mid_turn_injection
from reyn.runtime.turn_origin import MID_TURN_INJECTABLE, TurnOrigin
from tests._support.agent_session import make_session

AGENT = "5677-wire-rendering-agent"


def _make_session(tmp_path: Path, name: str):
    state_log = StateLog(tmp_path / f"{name}.wal")
    session = make_session(
        agent_name=AGENT, state_log=state_log, snapshot_path=tmp_path / f"{name}.json",
    )
    return session, state_log


# ---------------------------------------------------------------------------
# Coverage gate — every MID_TURN_INJECTABLE member has a real rendering
# ---------------------------------------------------------------------------


def test_every_mid_turn_injectable_member_has_a_rendering():
    """Tier 2: vacuity guard + completeness — MID_TURN_INJECTABLE is non-empty
    (fixture-stale guard) and every one of its members renders without
    raising. A future member added to MID_TURN_INJECTABLE without a matching
    branch in ``_render_mid_turn_injection`` fails HERE (an
    ``AssertionError`` from the function's own fail-loud fallback), not at
    the first real mid-turn injection in production."""
    assert MID_TURN_INJECTABLE, "MID_TURN_INJECTABLE is empty — fixture stale"

    _sample_payloads = {
        TurnOrigin.CLIENT_INPUT: {"text": "hello"},
        TurnOrigin.AGENT_REQUEST: {"from_agent": "peer", "request": "do X"},
        TurnOrigin.EXTERNAL_MESSAGE: {"text": "urgent update", "sender": "slack:U456"},
        TurnOrigin.HOOK: {"name": "on_idle", "text": "check the queue"},
    }
    assert set(_sample_payloads) == set(MID_TURN_INJECTABLE), (
        "this test's own sample payloads must cover exactly "
        "MID_TURN_INJECTABLE's current membership — update _sample_payloads "
        "before trusting this gate again"
    )
    for kind in MID_TURN_INJECTABLE:
        rendered = _render_mid_turn_injection(kind, _sample_payloads[kind])
        assert rendered["role"] in ("user", "system")
        assert isinstance(rendered["content"], str)


def test_a_kind_outside_mid_turn_injectable_has_no_declared_rendering():
    """Tier 2: deny-side sibling — a kind that is NOT eligible for mid-turn
    injection (CRON, #5747's own module docstring: operator-authored but
    delivered to a session with no client attached, not typed at a
    composer — a distinct question from HOOK's own eligibility) has no
    obligation to render here at all; calling ``_render_mid_turn_injection``
    for one is a caller bug this function correctly refuses rather than
    silently guessing a shape for."""
    assert TurnOrigin.CRON not in MID_TURN_INJECTABLE
    with pytest.raises(AssertionError):
        _render_mid_turn_injection(TurnOrigin.CRON, {"text": "0 * * * *"})


# ---------------------------------------------------------------------------
# CLIENT_INPUT — unchanged, role="user" (the #3792 founding shape)
# ---------------------------------------------------------------------------


def test_client_input_injection_renders_role_user_unchanged():
    """Tier 2: #3595's own regression pin — CLIENT_INPUT must keep rendering
    exactly as #3792 originally shipped it: role="user", bare text, no
    attribution prefix. Any change here would be a behavior change for the
    ONE producer this feature was built for (a human steering a running
    tool loop)."""
    rendered = _render_mid_turn_injection(TurnOrigin.CLIENT_INPUT, {"text": "hello"})
    assert rendered == {"role": "user", "content": "hello"}


# ---------------------------------------------------------------------------
# AGENT_REQUEST — role="system", attributed (the §0 requirement itself)
# ---------------------------------------------------------------------------


def test_agent_request_injection_renders_role_system_not_user():
    """Tier 2: accept side of architect's §0 requirement — an injected
    AGENT_REQUEST must NOT render as role="user" on the wire (would be
    indistinguishable from the operator's own text). Renders role="system"
    with the SAME [<kind>:<name>] attribution HOOK pushes already use.

    Reviewer strip (recorded here, executed manually before landing):
    removing the ``TurnOrigin.AGENT_REQUEST`` branch from
    ``_render_mid_turn_injection`` (falling through to the CLIENT_INPUT
    shape, or hardcoding role="user" unconditionally) makes this assertion
    go red — this is the ONE test #5677's own acceptance criteria named
    explicitly ("accept（描画）: 注入されたAGENT_REQUESTがwire上でrole=user
    では無い")."""
    rendered = _render_mid_turn_injection(
        TurnOrigin.AGENT_REQUEST,
        {"from_agent": "peer-agent", "request": "please redo step 1"},
    )
    assert rendered["role"] != "user"
    assert rendered["role"] == "system"
    assert rendered["content"] == "[agent_request:peer-agent] please redo step 1"


# ---------------------------------------------------------------------------
# EXTERNAL_MESSAGE — role="system", attributed by sender (owner ruling)
# ---------------------------------------------------------------------------


def test_external_message_injection_renders_role_system_not_user():
    """Tier 2: accept side of the owner's ruling (2026-09-02, verbatim:
    "入れる") to add EXTERNAL_MESSAGE to MID_TURN_INJECTABLE, overriding
    architect/lead-coder's own recommendation to exclude it — an injected
    EXTERNAL_MESSAGE must NOT render as role="user" on the wire (would be
    indistinguishable from the operator's own text, reopening #3595's own
    closed class one layer down). Renders role="system", attributed by
    ``sender`` (the individual peer — "slack:U456" — per TurnOrigin.
    EXTERNAL_MESSAGE's own docstring: sender is a strictly better source
    than the bare kind for a consumer that needs to name the transport).

    Reviewer strip (recorded here, to be executed manually before
    landing): removing the ``TurnOrigin.EXTERNAL_MESSAGE`` branch from
    ``_render_mid_turn_injection`` makes this assertion go red."""
    rendered = _render_mid_turn_injection(
        TurnOrigin.EXTERNAL_MESSAGE,
        {"text": "urgent update", "sender": "slack:U456"},
    )
    assert rendered["role"] != "user"
    assert rendered["role"] == "system"
    assert rendered["content"] == "[external_message:slack:U456] urgent update"


def test_external_message_injection_without_sender_falls_back_to_kind():
    """Tier 2: a payload with no ``sender`` is a REAL case (``mcp.server.
    send_to_agent_impl``'s own envelope carries none), not hypothetical —
    falls back to the bare ``kind`` rather than raising, per lead-coder's
    own recommendation."""
    rendered = _render_mid_turn_injection(
        TurnOrigin.EXTERNAL_MESSAGE, {"text": "hi from mcp peer"},
    )
    assert rendered["role"] == "system"
    assert rendered["content"] == "[external_message:external_message] hi from mcp peer"


# ---------------------------------------------------------------------------
# HOOK — role="system", attributed (#5747: owner-requested feature, real
# damage from the gap — see TurnOrigin.HOOK's own MID_TURN_INJECTABLE
# comment for the two incidents)
# ---------------------------------------------------------------------------


def test_hook_injection_renders_role_system_not_user():
    """Tier 2: accept side of #5747 — an injected HOOK push must NOT render
    as role="user" (would be indistinguishable from the operator's own
    text, reopening #3595's own closed class one layer down). Renders
    role="system" with the SAME ``[hook:name]`` attribution
    ``_handle_hook_message``'s own wake=true push already uses — an
    injected hook and a turn-starting one read identically on the wire.

    Reviewer strip (recorded here, to be executed manually before
    landing): removing the ``TurnOrigin.HOOK`` branch from
    ``_render_mid_turn_injection`` makes this assertion go red (the
    function's own fail-loud fallback raises instead)."""
    rendered = _render_mid_turn_injection(
        TurnOrigin.HOOK, {"name": "on_idle", "text": "check the queue"},
    )
    assert rendered["role"] != "user"
    assert rendered["role"] == "system"
    assert rendered["content"] == "[hook:on_idle] check the queue"


def test_hook_injection_without_name_falls_back_to_kind():
    """Tier 2: a payload with no ``name`` falls back to the bare ``kind``
    rather than raising — mirrors EXTERNAL_MESSAGE's own no-sender
    fallback above; an attribution missing the SPECIFIC hook is still a
    true, non-operator attribution."""
    rendered = _render_mid_turn_injection(TurnOrigin.HOOK, {"text": "check the queue"})
    assert rendered["role"] == "system"
    assert rendered["content"] == "[hook:hook] check the queue"


# ---------------------------------------------------------------------------
# End-to-end: Session.peek_mid_turn_injections() produces the SAME rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peek_mid_turn_injections_wire_shape_matches_kind(tmp_path):
    """Tier 2: end-to-end — the wire dict ``RouterLoop`` actually splices
    into ``messages`` (via ``Session.peek_mid_turn_injections``, the real
    seam wired into ``RouterHostAdapter``) carries the SAME per-kind
    rendering the unit tests above pin, not a re-derivation that could
    drift."""
    session, state_log = _make_session(tmp_path, "peek")
    await session._put_inbox(
        TurnOrigin.AGENT_REQUEST,
        {"from_agent": "peer-agent", "request": "please redo step 1", "chain_id": "c1"},
    )

    injections = await session.peek_mid_turn_injections()
    (only,) = injections
    assert only["wire"]["role"] == "system"
    assert only["wire"]["content"] == "[agent_request:peer-agent] please redo step 1"

    await state_log.aclose()


# ---------------------------------------------------------------------------
# End-to-end: history commit uses the SAME rendering as the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_committed_agent_request_injection_history_entry_is_not_role_user(tmp_path):
    """Tier 2: the DURABLE side of §0 — the history entry
    ``_commit_mid_turn_injection`` appends for an AGENT_REQUEST injection
    must ALSO not be role="user" (the same misattribution risk, persisted
    rather than momentary — a restored session reading its own history
    later must see the same non-operator attribution a live render showed).
    """
    session, state_log = _make_session(tmp_path, "commit")
    await session._put_inbox(
        TurnOrigin.AGENT_REQUEST,
        {"from_agent": "peer-agent", "request": "please redo step 1", "chain_id": "c1"},
    )
    injections = await session.peek_mid_turn_injections()
    (only,) = injections

    before_len = len(session.history)
    await session._commit_mid_turn_injection(only["msg_id"])

    assert len(session.history) == before_len + 1
    entry = session.history[-1]
    assert entry.role != "user"
    assert entry.role == "system"
    assert entry.content == "[agent_request:peer-agent] please redo step 1"

    await state_log.aclose()
