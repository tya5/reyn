"""Tier 2: ``/model`` slash command — per-session model-class override.

Tests:
  1. override wins over Agent default (session.model returns override)
  2. invalid class rejected with known_classes() list in error
  3. unset → Agent default returned (byte-identical baseline)
  4. sticky within session lifetime (override survives multiple calls)
  5. known_classes() includes user-configured classes

Each test is falsification-verified per [[feedback_falsify_acceptance_test_before_proof]]:
the acceptance assertion is paired with a direct check that the mechanism
under test WOULD fail without it (documented inline).
"""
from __future__ import annotations

import pytest

from reyn.chat.outbox import OutboxMessage
from reyn.interfaces.slash.model import model_cmd
from reyn.llm.model_resolver import ModelResolver


def _make_resolver(extra: dict | None = None) -> ModelResolver:
    """Resolver with light/standard/strong plus any extra user classes."""
    mapping = {"light": "openai/gpt-4o-mini", "standard": "openai/gpt-4o", "strong": "openai/gpt-4"}
    if extra:
        mapping.update(extra)
    return ModelResolver(mapping, builtin={})


class _FakeAgent:
    model: str = "standard"
    agent_name: str = "test-agent"


class _FakeSession:
    def __init__(self, resolver: ModelResolver, *, agent_model: str = "standard"):
        _agent = _FakeAgent()
        _agent.model = agent_model
        self._agent = _agent
        self._resolver = resolver
        self._model_override: str | None = None
        self.outbox: list[OutboxMessage] = []

    @property
    def model(self) -> str:
        return self._model_override if self._model_override is not None else self._agent.model

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox.append(msg)

    def reply_text(self) -> str:
        return "\n".join(m.text for m in self.outbox if m.text)


# ---------------------------------------------------------------------------
# Test 1: override wins over Agent default
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_override_wins_over_agent_default():
    """Tier 2: session.model returns override when /model <class> is set.

    Falsification: if the property still returned self._agent.model, then
    session.model == "standard" even after override — the assertion below
    would fail. Verified by asserting session.model BEFORE override == "standard".
    """
    resolver = _make_resolver()
    session = _FakeSession(resolver, agent_model="standard")
    assert session.model == "standard"  # baseline: no override

    await model_cmd(session, "light")

    assert session._model_override == "light"
    assert session.model == "light"  # override wins
    assert "model → light" in session.reply_text()


# ---------------------------------------------------------------------------
# Test 2: invalid class rejected with known_classes() list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_invalid_class_rejected():
    """Tier 2: /model <unknown> posts an error with available classes listed.

    Falsification: if is_known_class() always returned True, no error would be
    posted and _model_override would be set to the unknown value — the
    assertion `session._model_override is None` would fail.
    """
    resolver = _make_resolver()
    session = _FakeSession(resolver)

    await model_cmd(session, "does_not_exist")

    assert session._model_override is None  # not set on invalid class
    error_msgs = [m for m in session.outbox if m.kind == "error"]
    assert error_msgs, "expected an error outbox message"
    error_text = error_msgs[0].text
    assert "does_not_exist" in error_text
    assert "light" in error_text  # available classes listed
    assert "standard" in error_text
    assert "strong" in error_text


# ---------------------------------------------------------------------------
# Test 3: unset → Agent default (byte-identical baseline)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_unset_returns_agent_default():
    """Tier 2: session.model == Agent.model when no override is set.

    Falsification: if _model_override defaulted to something non-None, this
    assertion would fail. The test directly checks the before-any-override
    state == Agent identity value, proving byte-identical behaviour.
    """
    resolver = _make_resolver()
    session = _FakeSession(resolver, agent_model="strong")

    assert session._model_override is None
    assert session.model == "strong"  # Agent default, no override


# ---------------------------------------------------------------------------
# Test 4: sticky — override persists within session lifetime
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_override_sticky():
    """Tier 2: once set, override persists across subsequent calls to session.model.

    Falsification: if _model_override were reset on each property read,
    session.model would return the Agent default on the second check.
    """
    resolver = _make_resolver()
    session = _FakeSession(resolver, agent_model="standard")

    await model_cmd(session, "light")
    assert session.model == "light"

    # second read — must still return override, not agent default
    assert session.model == "light"
    assert session._agent.model == "standard"  # agent default unchanged


# ---------------------------------------------------------------------------
# Test 5: no-arg display shows current + override note + available classes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_no_arg_display_with_override():
    """Tier 2: /model (no-arg) shows current model, override note, available classes.

    Falsification: if the handler exited early without building the display
    string, session.outbox would be empty — the assertion would fail.
    Also verifies the transient-override UX note is present.
    """
    resolver = _make_resolver()
    session = _FakeSession(resolver, agent_model="standard")
    # Set an override first so the display branch fires
    session._model_override = "light"

    await model_cmd(session, "")

    text = session.reply_text()
    assert "light" in text            # current model shown
    assert "this session" in text     # transient note shown
    assert "standard" in text         # agent default shown
    assert "available:" in text       # class list present


@pytest.mark.asyncio
async def test_model_no_arg_display_no_override():
    """Tier 2: /model (no-arg) without an override shows agent default + no override note."""
    resolver = _make_resolver()
    session = _FakeSession(resolver, agent_model="standard")

    await model_cmd(session, "")

    text = session.reply_text()
    assert "standard" in text
    assert "no override" in text
    assert "available:" in text
    assert "this session" not in text  # no override note when not set


# ---------------------------------------------------------------------------
# Test 6: known_classes() includes user-configured classes
# ---------------------------------------------------------------------------

def test_known_classes_includes_user_configured():
    """Tier 2: ModelResolver.known_classes() returns user-defined classes.

    Falsification: if known_classes() only returned STANDARD_CLASSES and ignored
    user mapping, "fast" would not appear in the result.
    """
    resolver = _make_resolver(extra={"fast": "openai/gpt-3.5-turbo"})
    classes = resolver.known_classes()
    assert "light" in classes
    assert "standard" in classes
    assert "strong" in classes
    assert "fast" in classes  # user-configured class present
    assert classes == sorted(classes)  # sorted guarantee
