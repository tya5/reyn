"""Tier 2: ``/model`` slash command — per-session model-class override.

Three test groups:

A. ``Session.model`` property (real Session, public-surface assert):
   Exercises the actual production property — no stub copy. The FP-0043
   coherence the lead wanted to verify lives here.

B. ``model_cmd`` handler (mixed):
   - B1: Real Session with captured ``_put_outbox`` for paths that read
     ``session.model`` (no-arg display).
   - B2: Stub session for paths that don't read ``session.model`` (valid/
     invalid class dispatch) — stub has no ``model`` property so any
     accidental read would raise AttributeError, making the boundary explicit.

C. ``ModelResolver.known_classes()`` — no session needed.

Falsification notes (per [[feedback_falsify_acceptance_test_before_proof]]):
  Every test documents which assertion would fail if the mechanism were absent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.outbox import OutboxMessage
from reyn.chat.session import Session
from reyn.config import SafetyConfig, TimeoutConfig
from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash.model import model_cmd
from reyn.llm.model_resolver import ModelResolver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(tmp_path: Path, *, model: str = "standard") -> Session:
    """Minimal real Session with WAL."""
    return Session(
        agent_name="test_agent",
        model=model,
        state_log=StateLog(tmp_path / "state.wal"),
        safety=SafetyConfig(timeout=TimeoutConfig(chain_seconds=60.0)),
        snapshot_path=tmp_path / "snap.json",
    )


def _make_resolver(extra: dict | None = None) -> ModelResolver:
    mapping = {
        "light": "openai/gpt-4o-mini",
        "standard": "openai/gpt-4o",
        "strong": "openai/gpt-4",
    }
    if extra:
        mapping.update(extra)
    return ModelResolver(mapping, builtin={})


def _capture_outbox(session: Session) -> list[OutboxMessage]:
    """Replace session._put_outbox with a simple collector; return the list."""
    captured: list[OutboxMessage] = []

    async def _collect(msg: OutboxMessage) -> None:
        captured.append(msg)

    session._put_outbox = _collect  # type: ignore[method-assign]
    return captured


def _reply_text(msgs: list[OutboxMessage]) -> str:
    return "\n".join(m.text for m in msgs if m.text)


def _error_text(msgs: list[OutboxMessage]) -> str:
    return "\n".join(m.text for m in msgs if m.kind == "error" and m.text)


class _FakeSession:
    """Stub for handler tests that do NOT read session.model.

    Deliberately has NO ``model`` property — any accidental read raises
    AttributeError, making the test boundary explicit.  Model-state assertions
    belong in Group A (real Session).
    """

    def __init__(self, resolver: ModelResolver, *, agent_model: str = "standard"):
        from reyn.chat.agent import Agent as _Agent
        self._agent = _Agent(agent_name="test", model=agent_model)
        self._resolver = resolver
        self._model_override: str | None = None
        self.outbox: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox.append(msg)

    def error_text(self) -> str:
        return "\n".join(m.text for m in self.outbox if m.kind == "error" and m.text)


# ===========================================================================
# Group A: Session.model property — real Session, public-surface assert
# ===========================================================================

def test_session_model_returns_override_when_set(tmp_path):
    """Tier 2 (Group A): session.model returns override class when _model_override set.

    Falsification: if the property still returned self._agent.model, then after
    setting _model_override = "light", session.model would remain "standard"
    and the assertion below would fail.
    """
    session = _make_session(tmp_path, model="standard")
    assert session.model == "standard"  # baseline: no override

    session._model_override = "light"
    assert session.model == "light"  # override wins


def test_session_model_returns_agent_default_when_no_override(tmp_path):
    """Tier 2 (Group A): session.model == agent default when _model_override is None.

    Falsification: if _model_override were not initialised to None (e.g. defaulted
    to some class), session.model would not equal the construction-time model and
    this assertion would fail.
    """
    session = _make_session(tmp_path, model="strong")
    assert session.model == "strong"  # agent default, no override applied


def test_session_model_override_cleared_returns_agent_default(tmp_path):
    """Tier 2 (Group A): clearing _model_override restores agent default.

    Falsification: if clearing the override did not affect the property, the
    final assertion would still return "light" instead of "standard".
    """
    session = _make_session(tmp_path, model="standard")
    session._model_override = "light"
    assert session.model == "light"

    session._model_override = None
    assert session.model == "standard"  # agent default restored


# ===========================================================================
# Group B1: no-arg display — real Session (reads session.model)
# ===========================================================================

@pytest.mark.asyncio
async def test_model_cmd_no_arg_display_with_active_override(tmp_path):
    """Tier 2 (Group B1): /model (no-arg) with active override shows transient note.

    Uses real Session so session.model reads through the production property.
    Falsification: if the no-arg branch exited without posting, captured msgs
    would be empty and all assertions would fail.
    """
    session = _make_session(tmp_path, model="standard")
    session._resolver = _make_resolver()
    msgs = _capture_outbox(session)
    session._model_override = "light"

    await model_cmd(session, "")

    text = _reply_text(msgs)
    assert "light" in text
    assert "this session" in text  # transient-override UX note
    assert "available:" in text


@pytest.mark.asyncio
async def test_model_cmd_no_arg_display_no_override(tmp_path):
    """Tier 2 (Group B1): /model (no-arg) without override shows no transient note.

    Falsification: if the no-arg branch exited early, msgs would be empty.
    """
    session = _make_session(tmp_path, model="standard")
    session._resolver = _make_resolver()
    msgs = _capture_outbox(session)

    await model_cmd(session, "")

    text = _reply_text(msgs)
    assert "standard" in text
    assert "no override" in text
    assert "available:" in text
    assert "this session" not in text  # no transient note when not overridden


# ===========================================================================
# Group B2: valid/invalid dispatch — stub (does NOT read session.model)
# ===========================================================================

@pytest.mark.asyncio
async def test_model_cmd_valid_class_replies_confirmation():
    """Tier 2 (Group B2): /model <valid-class> posts confirmation reply.

    Stub session has no model property; if the handler accidentally read
    session.model it would raise AttributeError.
    Falsification: if model_cmd exited without calling _put_outbox, outbox
    would be empty and the assertion would fail.
    """
    resolver = _make_resolver()
    session = _FakeSession(resolver, agent_model="standard")

    await model_cmd(session, "light")

    texts = [m.text for m in session.outbox if m.text]
    assert texts, "expected a confirmation reply"
    combined = "\n".join(texts)
    assert "light" in combined
    assert "this session" in combined  # transient-override UX note


@pytest.mark.asyncio
async def test_model_cmd_invalid_class_posts_error_with_class_list():
    """Tier 2 (Group B2): /model <unknown> posts error listing available classes.

    Falsification: if is_known_class() always returned True, no error would be
    posted and error_text() would be empty — assertion would fail.
    """
    resolver = _make_resolver()
    session = _FakeSession(resolver)

    await model_cmd(session, "does_not_exist")

    error = session.error_text()
    assert error, "expected an error message"
    assert "does_not_exist" in error
    assert "light" in error
    assert "standard" in error
    assert "strong" in error
    # no success (non-error) messages
    success = [m for m in session.outbox if m.kind != "error"]
    assert not success, f"expected no success reply, got {success}"


# ===========================================================================
# Group C: ModelResolver.known_classes() — no session needed
# ===========================================================================

def test_known_classes_includes_user_configured():
    """Tier 2 (Group C): known_classes() returns sorted list including user-defined.

    Falsification: if known_classes() only returned STANDARD_CLASSES and ignored
    user mapping, "fast" would not appear — assertion would fail.
    """
    resolver = _make_resolver(extra={"fast": "openai/gpt-3.5-turbo"})
    classes = resolver.known_classes()
    assert "light" in classes
    assert "standard" in classes
    assert "strong" in classes
    assert "fast" in classes
    assert classes == sorted(classes)
