"""Tier 2: reyn.plugins.api — public plugin helper API (FP-0041 follow-up).

Pins the stable contract that webhook plugins consume:

  push_to_agent(target_agent, text, sender, reply_to=None,
                kind="user", extra_meta=None, registry=None)

Internal ``Session._put_inbox`` is deliberately private; the
helper is the documented path. Tests cover:

  1. Happy path: helper resolves registry, calls ensure_running,
     and pushes the right envelope shape.
  2. Optional ``reply_to`` propagates when set, absent when None.
  3. Optional ``extra_meta`` becomes the envelope's ``meta``.
  4. Non-default ``kind`` is forwarded to ``_put_inbox`` (= future
     A2A/MCP unify path).
  5. Custom ``registry`` override is honored (= tests stub out the
     process singleton).
  6. ``FileNotFoundError`` from registry propagates (= caller
     handles, e.g. webhook returns 503).

Tier 2 because the API is the stable contract for all webhook
plugins; a regression in envelope shape breaks every plugin's
integration with Reyn.
"""
from __future__ import annotations

import pytest

from reyn.plugins.api import push_to_agent
from reyn.runtime.transport import ExternalRef

# ── stub registry / session ───────────────────────────────────────────


class _StubSession:
    def __init__(self):
        self.pushed: list = []

    async def _put_inbox(self, kind, payload):
        self.pushed.append((kind, payload))
        return f"msg-{len(self.pushed)}"


class _StubRegistry:
    def __init__(self, *, missing: list[str] | None = None):
        self._sessions: dict[str, _StubSession] = {}
        self._missing = set(missing or [])

    async def ensure_running(self, name):
        if name in self._missing:
            raise FileNotFoundError(name)
        if name not in self._sessions:
            self._sessions[name] = _StubSession()
        return self._sessions[name]

    # FP-0043 S4b-5: deliver_to_agent now routes to a per-sender webhook session via
    # resolve_session + ensure_session_running. This single-agent stub collapses them
    # onto the one per-agent session (the per-sender routing itself is pinned by
    # test_webhook_routing); both return the SAME session the tests assert on.
    def resolve_session(self, name, transport, native_id):
        if name in self._missing:
            raise FileNotFoundError(name)
        if name not in self._sessions:
            self._sessions[name] = _StubSession()
        return self._sessions[name]

    def ensure_session_running(self, name, sid):
        return self._sessions.get(name)


# ── tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_to_agent_minimal_call():
    """Tier 2: a minimal call (= target_agent + text + sender) lands
    a ``kind="user"`` envelope with no reply_to / meta.
    """
    reg = _StubRegistry()
    await push_to_agent(
        target_agent="news",
        text="hello",
        sender="slack:U1",
        registry=reg,
    )
    sess = await reg.ensure_running("news")
    assert sess.pushed == [("user", {"text": "hello", "sender": "slack:U1"})]


@pytest.mark.asyncio
async def test_push_to_agent_with_reply_to():
    """Tier 2: ``reply_to`` propagates into the envelope so the
    outbox interceptor (= PR-D2) can route replies externally.
    """
    reg = _StubRegistry()
    ref = ExternalRef(transport="slack", destination={"channel": "C1"})
    await push_to_agent(
        target_agent="news",
        text="hello",
        sender="slack:U1",
        reply_to=ref,
        registry=reg,
    )
    sess = await reg.ensure_running("news")
    kind, payload = sess.pushed[0]
    assert kind == "user"
    assert payload["reply_to"] is ref


@pytest.mark.asyncio
async def test_push_to_agent_omits_reply_to_when_none():
    """Tier 2: when ``reply_to=None`` the envelope does NOT carry
    a ``reply_to`` key (= keeps the dispatch attribution code from
    re-checking a None value).
    """
    reg = _StubRegistry()
    await push_to_agent(
        target_agent="news",
        text="hello",
        sender="slack:U1",
        registry=reg,
    )
    sess = await reg.ensure_running("news")
    _, payload = sess.pushed[0]
    assert "reply_to" not in payload


@pytest.mark.asyncio
async def test_push_to_agent_extra_meta_propagates():
    """Tier 2: ``extra_meta`` is stored in the envelope as ``meta``.
    Used by future unification paths (= A2A chain_id, MCP request_id).
    """
    reg = _StubRegistry()
    await push_to_agent(
        target_agent="news",
        text="hello",
        sender="a2a:peer",
        extra_meta={"chain_id": "abc-123"},
        registry=reg,
    )
    sess = await reg.ensure_running("news")
    _, payload = sess.pushed[0]
    assert payload["meta"] == {"chain_id": "abc-123"}


@pytest.mark.asyncio
async def test_push_to_agent_kind_override():
    """Tier 2: non-default ``kind`` is forwarded to ``_put_inbox``.
    Reserved for future A2A / MCP unification; webhook plugins use
    the default ``"user"``.
    """
    reg = _StubRegistry()
    await push_to_agent(
        target_agent="news",
        text="please respond",
        sender="a2a:peer",
        kind="agent_request",
        registry=reg,
    )
    sess = await reg.ensure_running("news")
    kind, _ = sess.pushed[0]
    assert kind == "agent_request"


@pytest.mark.asyncio
async def test_push_to_agent_registry_override_for_tests():
    """Tier 2: the ``registry`` kwarg lets tests pass a stub instead
    of using the process-shared singleton. Production callers omit
    it; the singleton is fetched lazily inside the helper.
    """
    reg = _StubRegistry()
    await push_to_agent(
        target_agent="agent_a",
        text="x",
        sender="user:tui",
        registry=reg,
    )
    # Pushed to the supplied stub, not the global registry.
    session = await reg.ensure_running("agent_a")
    assert session.pushed, "expected at least one push to the stub session"


@pytest.mark.asyncio
async def test_push_to_agent_propagates_file_not_found():
    """Tier 2: when the registry can't resolve the agent (=
    operator typo in ``target_agent`` or agent not yet created),
    ``FileNotFoundError`` surfaces to the caller. Webhook plugins
    catch this to return 503 with a clear message.
    """
    reg = _StubRegistry(missing=["nonexistent"])
    with pytest.raises(FileNotFoundError):
        await push_to_agent(
            target_agent="nonexistent",
            text="x",
            sender="user:tui",
            registry=reg,
        )


@pytest.mark.asyncio
async def test_push_to_agent_full_envelope_shape():
    """Tier 2: a full-args call produces the documented envelope
    shape (= text + sender + reply_to + meta).
    """
    reg = _StubRegistry()
    ref = ExternalRef(transport="line", destination={"reply_token": "T1"})
    await push_to_agent(
        target_agent="news",
        text="hi",
        sender="line:user:U1",
        reply_to=ref,
        extra_meta={"trace_id": "abc"},
        registry=reg,
    )
    sess = await reg.ensure_running("news")
    kind, payload = sess.pushed[0]
    assert kind == "user"
    assert payload == {
        "text": "hi",
        "sender": "line:user:U1",
        "reply_to": ref,
        "meta": {"trace_id": "abc"},
    }


# ── list_agents / agent_exists ─────────────────────────────────────────


class _StubDiscoveryRegistry:
    """Registry stub exposing list_names + exists for discovery tests."""
    def __init__(self, names: list[str]):
        self._names = list(names)

    def list_names(self) -> list[str]:
        return sorted(self._names)

    def exists(self, name: str) -> bool:
        return name in self._names


def test_list_agents_returns_registry_names():
    """Tier 2: ``list_agents`` returns the registry's sorted
    ``list_names()``. Plugin authors use this at register_router
    time to validate config or discover available agents.
    """
    from reyn.plugins.api import list_agents
    reg = _StubDiscoveryRegistry(["zeta", "alpha", "beta"])
    assert list_agents(registry=reg) == ["alpha", "beta", "zeta"]


def test_list_agents_empty_when_no_agents():
    """Tier 2: a project with no agents on disk returns empty list."""
    from reyn.plugins.api import list_agents
    reg = _StubDiscoveryRegistry([])
    assert list_agents(registry=reg) == []


def test_agent_exists_true_for_known():
    """Tier 2: ``agent_exists`` matches the registry's ``exists``."""
    from reyn.plugins.api import agent_exists
    reg = _StubDiscoveryRegistry(["news_agent"])
    assert agent_exists("news_agent", registry=reg) is True
    assert agent_exists("missing", registry=reg) is False


def test_agent_exists_returns_false_on_registry_error():
    """Tier 2: ``agent_exists`` is defensive — registry exception
    yields False rather than propagating. A boot-time registry
    hiccup shouldn't crash plugin discovery.
    """
    from reyn.plugins.api import agent_exists

    class _BrokenRegistry:
        def exists(self, name):
            raise RuntimeError("registry broken")

    assert agent_exists("anything", registry=_BrokenRegistry()) is False


# ── make_sender ────────────────────────────────────────────────────────


def test_make_sender_basic_transport_id():
    """Tier 2: minimal call produces ``<transport>:<external_id>``."""
    from reyn.plugins.api import make_sender
    assert make_sender("slack", "U456") == "slack:U456"


def test_make_sender_with_display():
    """Tier 2: ``display`` appends as the trailing segment."""
    from reyn.plugins.api import make_sender
    assert make_sender("slack", "U456", display="bob") == "slack:U456:bob"


def test_make_sender_with_source_scope():
    """Tier 2: ``source_scope`` inserts between transport and id —
    LINE 1:1 chat shape per ``_format_sender_label`` expectations.
    """
    from reyn.plugins.api import make_sender
    assert (
        make_sender("line", "U456", source_scope="user")
        == "line:user:U456"
    )


def test_make_sender_line_group_full_shape():
    """Tier 2: a LINE group sender combines transport + source_scope
    + group_id + posting user. Pins the documented shape that
    ``_format_sender_label`` recognises.
    """
    from reyn.plugins.api import make_sender
    sender = make_sender(
        "line", "G999", source_scope="group", display="U456",
    )
    assert sender == "line:group:G999:U456"


def test_make_sender_omits_falsy_display_and_scope():
    """Tier 2: empty / None ``display`` and ``source_scope`` are
    NOT included (= no trailing colons, no doubled separators).
    """
    from reyn.plugins.api import make_sender
    assert make_sender("slack", "U1", display=None) == "slack:U1"
    assert make_sender("slack", "U1", display="") == "slack:U1"
    assert (
        make_sender("slack", "U1", source_scope=None, display=None)
        == "slack:U1"
    )
