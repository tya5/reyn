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

from reyn.config import SafetyConfig, TimeoutConfig
from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash import SlashContext
from reyn.interfaces.slash.model import model_cmd
from reyn.llm.model_resolver import ModelResolver
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.slash import slash_ctx

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(tmp_path: Path, *, model: str = "standard") -> Session:
    """Minimal real Session with WAL."""
    return make_session(
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
    return ModelResolver(mapping)


def _ctx(session) -> "SlashContext":
    """The context the production dispatch hands a slash handler.

    #3595 S4 moved the reply path onto the client transport, so the recording
    transport — not the session outbox — is where a reply lands. Read it back
    through ``ctx.transport``.
    """
    return slash_ctx(session)


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
        from reyn.runtime.agent import Agent as _Agent
        self._agent = _Agent(agent_name="test", model=agent_model)
        self._resolver = resolver
        self._model_override: str | None = None

    def _rebuild_derived_model_engines_for_model(self) -> None:
        # #1752 / #3785: no-op on the stub — it has no turn_budget engine,
        # router host, or compaction controller. The real rebuilds (both
        # folded into this one accessor) are exercised in Group D against a
        # real Session.
        pass



# ===========================================================================
# Group A: Session.model property — real Session, public-surface assert
# ===========================================================================

def test_session_model_returns_override_when_set(tmp_path):
    """Tier 2: session.model returns override class when _model_override set.

    Falsification: if the property still returned self._agent.model, then after
    setting _model_override = "light", session.model would remain "standard"
    and the assertion below would fail.
    """
    session = _make_session(tmp_path, model="standard")
    assert session.model == "standard"  # baseline: no override

    session._model_override = "light"
    assert session.model == "light"  # override wins


def test_session_model_returns_agent_default_when_no_override(tmp_path):
    """Tier 2: session.model == agent default when _model_override is None.

    Falsification: if _model_override were not initialised to None (e.g. defaulted
    to some class), session.model would not equal the construction-time model and
    this assertion would fail.
    """
    session = _make_session(tmp_path, model="strong")
    assert session.model == "strong"  # agent default, no override applied


def test_session_model_override_cleared_returns_agent_default(tmp_path):
    """Tier 2: clearing _model_override restores agent default.

    Falsification: if clearing the override did not affect the property, the
    final assertion would still return "light" instead of "standard".
    """
    session = _make_session(tmp_path, model="standard")
    session._model_override = "light"
    assert session.model == "light"

    session._model_override = None
    assert session.model == "standard"  # agent default restored


def test_session_active_model_class_returns_override_when_set(tmp_path):
    """Tier 2: active_model_class() returns the override class name when set.

    Falsification: if the method did not short-circuit on _model_override, the
    reverse-lookup loop would run and might return a different value (or None).
    """
    session = _make_session(tmp_path, model="standard")
    session._resolver = _make_resolver()
    session._model_override = "light"
    assert session.active_model_class() == "light"


def test_session_active_model_class_reverse_lookup_no_override(tmp_path):
    """Tier 2: active_model_class() reverse-looks up the class when no override.

    The agent's model is the full LiteLLM model ID "openai/gpt-4o"; the resolver
    maps "standard" → "openai/gpt-4o". Pre-fix, callers compared this full ID
    against class names and got no match (▸ never appeared in the model picker).

    Falsification: if the reverse-lookup were absent (returning None unconditionally
    on no-override), this assertion would fail.
    """
    session = _make_session(tmp_path, model="openai/gpt-4o")
    session._resolver = _make_resolver()  # "standard" → "openai/gpt-4o"
    # No override set — fresh session has no model_override by construction.
    assert session.active_model_class() == "standard"


def test_session_active_model_class_returns_none_for_unknown_model(tmp_path):
    """Tier 2: active_model_class() returns None when the agent model is not in
    any configured class (= passthrough / custom model).

    Falsification: if the method returned the raw model ID instead of None, the
    model picker would show a phantom ▸ on a non-class entry.
    """
    session = _make_session(tmp_path, model="custom/bespoke-model-v9")
    session._resolver = _make_resolver()  # no "custom/bespoke-model-v9" entry
    assert session.active_model_class() is None


# ===========================================================================
# Group B1: no-arg display — real Session (reads session.model)
# ===========================================================================

@pytest.mark.asyncio
async def test_model_cmd_no_arg_display_with_active_override(tmp_path):
    """Tier 2: /model (no-arg) with active override shows transient note.

    Uses real Session so session.model reads through the production property.
    Falsification: if the no-arg branch exited without posting, captured msgs
    would be empty and all assertions would fail.
    """
    session = _make_session(tmp_path, model="standard")
    session._resolver = _make_resolver()
    session._model_override = "light"

    ctx = _ctx(session)
    await model_cmd(ctx, "")

    text = _reply_text(ctx.transport.displayed)
    assert "light" in text
    assert "this session" in text  # transient-override UX note
    assert "available:" in text


@pytest.mark.asyncio
async def test_model_cmd_no_arg_display_no_override(tmp_path):
    """Tier 2: /model (no-arg) without override shows no transient note.

    Falsification: if the no-arg branch exited early, msgs would be empty.
    """
    session = _make_session(tmp_path, model="standard")
    session._resolver = _make_resolver()
    
    ctx = _ctx(session)
    await model_cmd(ctx, "")

    text = _reply_text(ctx.transport.displayed)
    assert "standard" in text
    assert "no override" in text
    assert "available:" in text
    assert "this session" not in text  # no transient note when not overridden


# ===========================================================================
# Group B2: valid/invalid dispatch — stub (does NOT read session.model)
# ===========================================================================

@pytest.mark.asyncio
async def test_model_cmd_valid_class_replies_confirmation():
    """Tier 2: /model <valid-class> posts confirmation reply.

    Stub session has no model property; if the handler accidentally read
    session.model it would raise AttributeError.
    Falsification: if model_cmd exited without calling _put_outbox, outbox
    would be empty and the assertion would fail.
    """
    resolver = _make_resolver()
    session = _FakeSession(resolver, agent_model="standard")

    ctx = _ctx(session)
    await model_cmd(ctx, "light")

    texts = [m.text for m in ctx.transport.displayed if m.text]
    assert texts, "expected a confirmation reply"
    combined = "\n".join(texts)
    assert "light" in combined
    assert "this session" in combined  # transient-override UX note


@pytest.mark.asyncio
async def test_model_cmd_invalid_class_posts_error_with_class_list():
    """Tier 2: /model <unknown> posts error listing available classes.

    Falsification: if is_known_class() always returned True, no error would be
    posted and error_text() would be empty — assertion would fail.
    """
    resolver = _make_resolver()
    session = _FakeSession(resolver)

    ctx = _ctx(session)
    await model_cmd(ctx, "does_not_exist")

    error = ctx.transport.error_text()
    assert error, "expected an error message"
    assert "does_not_exist" in error
    assert "light" in error
    assert "standard" in error
    assert "strong" in error
    # no success (non-error) messages
    success = [m for m in ctx.transport.displayed if m.kind != "error"]
    assert not success, f"expected no success reply, got {success}"


# ===========================================================================
# Group C: ModelResolver.known_classes() — no session needed
# ===========================================================================

def test_known_classes_includes_user_configured():
    """Tier 2: known_classes() returns sorted list including user-defined.

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


# ===========================================================================
# Group D: #1752 — per-turn budget consumers track the live /model override
# ===========================================================================

@pytest.mark.asyncio
async def test_turn_budget_engine_rebuilt_on_model_switch(tmp_path):
    """Tier 2: /model switch rebuilds the chat turn_budget engine for the new
    model (rebuild-on-switch; the engine bakes derived headroom at construction).

    #3671 follow-up: ``session._router_host._turn_budget_engine`` is a LAZY
    cache (``_TURN_BUDGET_ENGINE_UNSET`` sentinel until first reference —
    reading it directly here, before anything triggers a real build, returns
    the sentinel, not "the construction-time engine object"). The rebuild
    call (``set_turn_budget_engine``) sets it EAGERLY, so ``before`` (still
    the sentinel) and ``after`` (a real value, possibly ``None`` for a
    non-viable resolved model) are still reliably distinct either way.

    Falsification: if model_cmd did not call
    ``session._rebuild_derived_model_engines_for_model()``, ``after`` would
    still be the same untouched sentinel as ``before`` and the identity
    assertion below would fail (after is before).
    """
    session = _make_session(tmp_path, model="standard")
    session._resolver = _make_resolver()
    before = session.router_host._turn_budget_engine

    ctx = _ctx(session)
    await model_cmd(ctx, "strong")

    after = session.router_host._turn_budget_engine
    assert after is not before  # rebuilt (or explicitly re-evaluated) for the new model


@pytest.mark.asyncio
async def test_compaction_engine_rebuilt_on_model_switch(tmp_path):
    """Tier 2: #3785 — /model switch rebuilds the compaction engine for the
    new model. Before this fix, compaction never tracked a ``/model`` switch
    at all — it kept compacting on whatever model the session started with.

    ``CompactionController._engine`` is a lazy property (#3671): reading it
    for ``before`` triggers the FIRST real build (against "standard"),
    exactly like a real first compaction trigger would. The rebuild call
    only invalidates the cache (stays lazy — #3671's own discipline), so
    ``after`` is a SECOND build, against whatever ``session.model`` is at
    that later reference — "strong", following the switch.

    Falsification: if model_cmd did not call
    ``session._rebuild_derived_model_engines_for_model()``, ``after`` would
    be the exact same cached object as ``before`` (still resolving against
    "standard") and both assertions below would fail.
    """
    session = _make_session(tmp_path, model="standard")
    session._resolver = _make_resolver()
    before = session._compaction_controller._engine
    before_model = before.model

    ctx = _ctx(session)
    await model_cmd(ctx, "strong")

    after = session._compaction_controller._engine
    assert after is not before  # rebuilt, not the same cached engine
    assert after.model != before_model  # ...and resolves against the NEW model


# ===========================================================================
# Group E: #4685 — /model switch to a bare (no provider-prefix) model name
# ===========================================================================

@pytest.mark.asyncio
async def test_turn_budget_engine_rebuild_does_not_raise_for_a_bare_model_name(tmp_path):
    """Tier 2: #4685 — real bug, owner's real config shape. Every model
    value in ``_make_resolver()`` (the fixture Group D's own tests reuse)
    happens to carry a provider prefix ("openai/gpt-4o", ...), which is
    EXACTLY why the pre-#4685 bug never showed up here: the buggy call
    pre-resolved the class into its NAME, then fed that NAME to an
    internal EMPTY resolver — but a NAME containing "/" still passes that
    empty resolver's own passthrough branch (``ModelResolver.resolve``
    checks "/" in name before it ever needs a class table), so the
    coincidence hid the defect. The owner's real ``reyn.yaml`` used a bare
    model id with no provider prefix (``gpt-5.6-terra``) — this test
    reproduces exactly that shape: a class ("terra") whose resolved model
    value carries NO "/", so a caller that (incorrectly) hands the empty
    resolver a bare NAME instead of the real CLASS+resolver pair hits
    ``ModelResolver.resolve``'s class-position raise
    ("model class 'gpt-5.6-terra' not found among known classes (none)").

    Two independent bugs stacked to break this, both fixed in the same
    PR: (1) this call site pre-resolved the class and dropped the
    resolver (architect's/lead-coder's own diagnosis); (2)
    ``TurnBudgetEngine.__init__`` ITSELF double-resolves — it resolves
    ``model`` once into ``self._model``, then passes that
    ALREADY-RESOLVED name to ``compute_turn_budget``, which resolves it
    a SECOND time; a resolved NAME with no "/" is never itself a
    declared class key, so the second resolve always raised regardless
    of whether the real resolver reached this far (found while writing
    THIS test — not in the original issue/diagnosis, which only reached
    the Session-level bug).

    Only the PUBLIC surface (construction did not raise, and a real
    engine — not None — resulted) is asserted; ``TurnBudgetEngine``
    exposes no public accessor for the resolved model string, and
    reading ``._model`` would be a private-state assertion."""
    session = _make_session(tmp_path, model="standard")
    session._resolver = _make_resolver({"terra": {"model": "gpt-5.6-terra"}})

    ctx = _ctx(session)
    await model_cmd(ctx, "terra")  # must not raise

    engine = session.router_host._turn_budget_engine
    assert engine is not None, "a viable model must produce a real engine, not None"


def test_lazy_startup_turn_budget_engine_does_not_raise_for_a_bare_model_name(tmp_path):
    """Tier 2: #4685 — the SECOND, independent Session-level call site
    with the identical defect shape (``Session``'s lazy
    ``_build_chat_turn_budget_engine`` closure, built at RouterHostAdapter
    construction and realized on first reference — #3671's deferred-build
    discipline). Not named in the original issue/diagnosis; found while
    fixing the first site. Also exercises the SAME ``TurnBudgetEngine``
    double-resolve bug the sibling test above documents.

    An override already in effect BEFORE the lazy engine is first
    referenced (a session resumed/constructed with a prior ``/model``
    switch already applied, whose first force-close check comes later —
    the realistic shape #3671's own deferred-build discipline describes)
    reproduces the same bug independently of the ``/model`` command path
    tested above."""
    session = _make_session(tmp_path, model="standard")
    session._resolver = _make_resolver({"terra": {"model": "gpt-5.6-terra"}})
    session._model_override = "terra"

    engine = session.router_host._ensure_turn_budget_engine()  # must not raise
    assert engine is not None
