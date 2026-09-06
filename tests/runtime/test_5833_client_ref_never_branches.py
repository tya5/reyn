"""Tier 2: #5833 — ``client_ref`` is an opaque correlation token the SERVER
receives, stores in ``meta``, and echoes back VERBATIM. It must never
change server behavior, whatever value a client sends.

The design ruling (lead-coder, #5833 issue thread) added this as an
acceptance line on top of the chosen fix (client_ref-in-meta over
identity-in-meta, option (b) rejected precisely because ``client_ref`` is
"client-supplied opaque junk" — blurring it with identity would make trust
flow from the classified side): "server は client_ref を一度も解釈しない
こと ... server 側に client_ref で分岐する行が1つでも在れば、それがこの
設計の破れ". Tested here by driving :meth:`Session.submit_user_text` with
four deliberately adversarial values — an arbitrary string, another
client's own former ref, an empty string, and a huge one — and asserting
the OBSERVABLE behavior (the emitted event's shape, the returned msg_id,
the inbox payload) is identical in every dimension EXCEPT the one field
that carries the value through unchanged.

No unittest.mock/AsyncMock/patch — a real ``Session`` (``tests/_support/
agent_session.py``'s own construction helper, the same one ``test_user_echo_
broadcast.py`` uses for the sibling #3300 audit-event contract) and a real
subscriber callback.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import SafetyConfig, TimeoutConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.events import settle


def _make_session(tmp_path: Path, *, agent_name: str = "test_agent") -> Session:
    session = make_session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        safety=SafetyConfig(timeout=TimeoutConfig(chain_seconds=60.0)),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )
    session.register_intervention_listener("test")
    return session


class _EventSink:
    """A real (non-mock) audit-event subscriber — same shape as
    ``test_user_echo_broadcast.py``'s own ``_EventSink``."""

    def __init__(self) -> None:
        self.events: list = []

    def __call__(self, event) -> None:
        self.events.append(event)


async def _user_submitted_event(session: Session, sink: _EventSink):
    await settle(session)
    matches = [e for e in sink.events if e.type == "user_submitted"]
    assert matches, "no user_submitted event observed"
    return matches[-1]


# The four adversarial values the design ruling itself named verbatim:
# "任意の値（他 client の値・空・巨大・記号）".
_ADVERSARIAL_CLIENT_REFS = pytest.mark.parametrize(
    "client_ref",
    [
        pytest.param("local:deadbeefcafebabe0000000000000001", id="arbitrary"),
        pytest.param("local:not-my-own-id-belongs-to-another-client", id="foreign"),
        pytest.param("", id="empty"),
        pytest.param("x" * 100_000, id="huge"),
        pytest.param("'; DROP TABLE sessions; --<script>", id="symbols"),
    ],
)


@_ADVERSARIAL_CLIENT_REFS
@pytest.mark.asyncio
async def test_submit_user_text_never_branches_on_client_ref(
    tmp_path: Path, client_ref: str,
) -> None:
    """Tier 2: whatever ``client_ref`` a caller sends, the enqueue/emit
    shape is IDENTICAL to the no-``client_ref`` baseline — text, chain_id
    presence, and the returned msg_id's own shape are all independent of
    the value. The single permitted difference is ``meta.client_ref``
    itself, and only when non-empty (an empty string round-trips too, but
    is checked separately below since it is falsy and easy to conflate
    with "absent")."""
    session = _make_session(tmp_path)
    sink = _EventSink()
    session.subscribe_audit_events(sink)

    msg_id = await session.submit_user_text("hello", client_ref=client_ref)

    assert isinstance(msg_id, str) and msg_id, (
        "a client_ref value must never change whether/how msg_id is assigned"
    )
    event = await _user_submitted_event(session, sink)
    assert event.data.get("text") == "hello"
    assert event.data.get("msg_id") == msg_id
    meta = event.data.get("meta") or {}
    assert meta.get("client_ref") == client_ref, (
        "the value must round-trip VERBATIM, byte-for-byte, never normalized "
        "or rejected"
    )


@pytest.mark.asyncio
async def test_client_ref_is_the_only_diff_between_two_otherwise_identical_submits(
    tmp_path: Path,
) -> None:
    """Tier 2: the sharpest form of "never branches" — two submissions,
    identical in every other respect, differing ONLY in ``client_ref``,
    produce events identical in every field except ``msg_id`` (a fresh id
    per call, unrelated to client_ref) and ``meta.client_ref`` itself."""
    session = _make_session(tmp_path)
    sink = _EventSink()
    session.subscribe_audit_events(sink)

    await session.submit_user_text("same text", client_ref="local:aaa")
    ev_a = await _user_submitted_event(session, sink)
    await session.submit_user_text("same text", client_ref="local:zzz-totally-different-shape")
    ev_b = await _user_submitted_event(session, sink)

    assert ev_a.data.get("text") == ev_b.data.get("text")
    assert ev_a.data.get("msg_id") != ev_b.data.get("msg_id"), (
        "msg_id assignment must be independent of client_ref's value"
    )
    meta_a = dict(ev_a.data.get("meta") or {})
    meta_b = dict(ev_b.data.get("meta") or {})
    del meta_a["client_ref"]
    del meta_b["client_ref"]
    assert meta_a == meta_b, (
        f"only client_ref may differ between these two events' meta — got "
        f"{meta_a!r} vs {meta_b!r}"
    )


@pytest.mark.asyncio
async def test_omitting_client_ref_entirely_is_unaffected(tmp_path: Path) -> None:
    """Tier 2: control arm — a caller that never passes ``client_ref`` at
    all (every pre-#5833 call site, and most callers going forward: the
    plain REPL, agent-to-agent submission) gets EXACTLY the pre-#5833
    shape — no ``client_ref`` key appears in ``meta`` at all, not even as
    ``None``."""
    session = _make_session(tmp_path)
    sink = _EventSink()
    session.subscribe_audit_events(sink)

    await session.submit_user_text("plain submit, no client_ref")

    event = await _user_submitted_event(session, sink)
    meta = event.data.get("meta") or {}
    assert "client_ref" not in meta, (
        f"omitting client_ref must not synthesize the key at all — got {meta!r}"
    )
