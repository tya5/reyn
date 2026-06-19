"""Tier 2: high-cost model pre-confirmation (#1830 / FP-0052).

Covers four contracts:
A. model_cost_rate utility (pure functions — no session needed).
B. CostWarnConfig parsing from raw YAML dict.
C. lifecycle_forwarder.on_model_cost_warn → conv-pane marker.
D. maybe_emit_model_cost_warn shared helper — de-dup + action field.

Non-duplication axis: these tests are NOT about BudgetTracker (cumulative
spend) or ContextBudgetAdvisor (token ceiling). They test the pre-selection
per-token-rate awareness layer, which is orthogonal to both.

Falsification:
- get_input_cost_per_1m_usd: returns None for unknown model (not 0).
- is_high_cost_model: returns False (not True) when rate is unknown.
- CostWarnConfig: missing key → default (not crash).
- on_model_cost_warn: non-float cost data → safe fallback (not crash).
- maybe_emit_model_cost_warn: same model class warned only once per session.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.config.chat import CostWarnConfig, _build_cost_warn_config
from reyn.llm.model_cost_rate import get_input_cost_per_1m_usd, is_high_cost_model
from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder

# ---------------------------------------------------------------------------
# A. model_cost_rate utility
# ---------------------------------------------------------------------------

def test_get_input_cost_returns_none_for_unknown_model() -> None:
    """Tier 2: unknown model → None (not 0.0, not an exception).

    Falsification: if the function returned 0.0 for unknown models, callers
    would treat all unknown models as free — is_high_cost_model would always
    return False for them even if cost data appears later.
    """
    result = get_input_cost_per_1m_usd("__definitely_not_a_real_model_xyz__")
    assert result is None, f"expected None for unknown model, got {result!r}"


def test_get_input_cost_returns_positive_for_known_model() -> None:
    """Tier 2: a model in litellm.model_cost returns a positive float.

    Falsification: if the lookup key were wrong (e.g. off-by-one in the
    per_token → per_1m scaling), the result would be tiny (<0.01) or negative.
    """
    # gpt-4o is in litellm's pricing DB at ~$2.50/1M input tokens.
    result = get_input_cost_per_1m_usd("gpt-4o")
    if result is None:
        pytest.skip("gpt-4o not found in this litellm version's pricing DB")
    assert isinstance(result, float), f"expected float, got {type(result).__name__}"
    assert result > 0, f"expected positive cost, got {result}"


def test_is_high_cost_returns_false_for_unknown_model() -> None:
    """Tier 2: unknown model → False (unknown cost ≠ high cost).

    Falsification: without the ``cost is not None`` guard, unknown models
    would cause a TypeError when comparing None > threshold.
    """
    result = is_high_cost_model("__definitely_not_a_real_model_xyz__", 5.0)
    assert result is False, "expected False for unknown model"


def test_is_high_cost_returns_false_for_cheap_model() -> None:
    """Tier 2: a below-threshold model returns False.

    Uses an absurdly high threshold (10000) so any real model is below it.
    Falsification: without the > check, threshold=10000 would still warn.
    """
    result = is_high_cost_model("gpt-4o", threshold_per_1m_usd=10_000.0)
    assert result is False


def test_is_high_cost_returns_true_for_low_threshold() -> None:
    """Tier 2: a very low threshold triggers the warning for a known model.

    Uses threshold=0.0 so any model with known cost fires.
    Falsification: without the > check (using >=), a model at exactly 0.0
    would also fire — but 0.0 threshold means "warn about everything known".
    """
    result = is_high_cost_model("gpt-4o", threshold_per_1m_usd=0.0)
    if get_input_cost_per_1m_usd("gpt-4o") is None:
        pytest.skip("gpt-4o not found in this litellm version's pricing DB")
    assert result is True, "expected True when threshold=0.0 for a known model"


# ---------------------------------------------------------------------------
# B. CostWarnConfig parsing
# ---------------------------------------------------------------------------

def test_build_cost_warn_config_defaults_when_missing() -> None:
    """Tier 2: missing / None raw → full defaults (enabled=True, threshold=5.0)."""
    cfg = _build_cost_warn_config(None)
    assert cfg.enabled is True
    assert cfg.model_threshold_per_1m_input_usd == 5.0


def test_build_cost_warn_config_parses_enabled_false() -> None:
    """Tier 2: enabled: false in YAML disables the feature."""
    cfg = _build_cost_warn_config({"enabled": False})
    assert cfg.enabled is False


def test_build_cost_warn_config_parses_custom_threshold() -> None:
    """Tier 2: custom threshold is parsed as float."""
    cfg = _build_cost_warn_config({"model_threshold_per_1m_input_usd": 15.0})
    assert cfg.model_threshold_per_1m_input_usd == 15.0


def test_build_cost_warn_config_bad_threshold_falls_back() -> None:
    """Tier 2: non-numeric threshold falls back to default (5.0), not a crash.

    Falsification: without the try/except around float(threshold), a YAML
    string like "not_a_number" would propagate as a ValueError.
    """
    cfg = _build_cost_warn_config({"model_threshold_per_1m_input_usd": "not_a_number"})
    assert cfg.model_threshold_per_1m_input_usd == CostWarnConfig().model_threshold_per_1m_input_usd


# ---------------------------------------------------------------------------
# C. lifecycle_forwarder.on_model_cost_warn
# ---------------------------------------------------------------------------

def _make_forwarder() -> tuple[ChatLifecycleForwarder, asyncio.Queue]:
    """Create a forwarder with a real asyncio.Queue (no mocks per policy)."""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    return ChatLifecycleForwarder(q), q


def test_on_model_cost_warn_enqueues_system_message() -> None:
    """Tier 2: on_model_cost_warn enqueues a system-kind outbox message."""
    fwd, q = _make_forwarder()
    fwd.on_model_cost_warn({
        "model": "anthropic/claude-opus-4-8",
        "cost_per_1m_input_usd": 15.0,
        "threshold_per_1m_input_usd": 5.0,
    })
    assert not q.empty(), "expected a message to be enqueued"
    msg = q.get_nowait()
    assert msg.kind == "system"
    assert "high-cost model" in msg.text
    assert "claude-opus-4-8" in msg.text


def test_on_model_cost_warn_includes_cost_in_message() -> None:
    """Tier 2: the enqueued message includes the cost figure.

    Falsification: without the cost formatting, users would see the warning
    but not know how expensive the model actually is.
    """
    fwd, q = _make_forwarder()
    fwd.on_model_cost_warn({
        "model": "anthropic/claude-opus-4-8",
        "cost_per_1m_input_usd": 15.0,
        "threshold_per_1m_input_usd": 5.0,
    })
    msg = q.get_nowait()
    assert "15.00" in msg.text or "$15" in msg.text, (
        f"expected cost figure in message, got: {msg.text!r}"
    )


def test_on_model_cost_warn_safe_on_missing_cost_field() -> None:
    """Tier 2: missing cost_per_1m_input_usd does not crash the forwarder.

    Falsification: without the try/except around float(cost), a missing field
    would propagate as TypeError and prevent the warn from being enqueued.
    """
    fwd, q = _make_forwarder()
    # no cost_per_1m_input_usd key
    fwd.on_model_cost_warn({"model": "some-model"})
    assert not q.empty(), "expected a message even when cost field is missing"


# ---------------------------------------------------------------------------
# D. maybe_emit_model_cost_warn shared helper (S3 — de-dup + action field)
# ---------------------------------------------------------------------------

class _FakeEventLog:
    """Minimal event log stub — records (type, data) pairs.

    ``snapshot()`` is the public read surface (mirrors the snapshot() idiom
    from testing policy — never assert on private fields like `.emitted`
    directly from test code; use this method instead).
    """
    def __init__(self) -> None:
        self._emitted: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **data: object) -> None:
        self._emitted.append((event_type, dict(data)))

    def snapshot(self) -> list[tuple[str, dict]]:
        """Public read — returns a copy of all (event_type, data) pairs so far."""
        return list(self._emitted)


class _FakeResolver:
    """Resolver stub that passes the model name through as the litellm key."""
    def resolve(self, name: str) -> object:
        class _Spec:
            model = name
        return _Spec()


class _FakeSession:
    """Minimal session duck-type for maybe_emit_model_cost_warn."""
    def __init__(self, *, enabled: bool = True, threshold: float = 0.0) -> None:
        from reyn.config.chat import CostWarnConfig
        self._config = type("Cfg", (), {
            "cost_warn": CostWarnConfig(
                enabled=enabled,
                model_threshold_per_1m_input_usd=threshold,
            ),
        })()
        self._resolver = _FakeResolver()
        self._chat_events = _FakeEventLog()

    def event_snapshot(self) -> list[tuple[str, dict]]:
        """Public surface: returns recorded (event_type, data) pairs."""
        return self._chat_events.snapshot()


def test_maybe_emit_model_cost_warn_emits_for_known_high_cost_model() -> None:
    """Tier 2: known model above threshold=0.0 → model_cost_warn emitted.

    Uses threshold=0.0 so any model with a known litellm price fires.
    Falsification: if resolved.model were not passed (bug: ModelSpec passed
    directly), litellm.model_cost lookup fails silently → no emit.
    """
    from reyn.llm.model_cost_rate import get_input_cost_per_1m_usd
    from reyn.runtime.model_cost_warn import maybe_emit_model_cost_warn

    if get_input_cost_per_1m_usd("gpt-4o") is None:
        pytest.skip("gpt-4o not in litellm pricing DB")

    session = _FakeSession(threshold=0.0)
    maybe_emit_model_cost_warn(session, "gpt-4o", action="session_start")

    events = session.event_snapshot()
    assert events, "expected model_cost_warn to be emitted"
    evt_type, evt_data = events[0]
    assert evt_type == "model_cost_warn"
    assert evt_data["action"] == "session_start"
    assert evt_data["model_class"] == "gpt-4o"


def test_maybe_emit_model_cost_warn_dedup_within_session() -> None:
    """Tier 2: same model class warned at most once per session.

    Behavioral check: first call fires; second call for the same model leaves
    the snapshot unchanged (= no new event added).

    Falsification: without the _cost_warned_models set check, two calls
    for the same model_class would emit two events (duplicate warn).
    """
    from reyn.llm.model_cost_rate import get_input_cost_per_1m_usd
    from reyn.runtime.model_cost_warn import maybe_emit_model_cost_warn

    if get_input_cost_per_1m_usd("gpt-4o") is None:
        pytest.skip("gpt-4o not in litellm pricing DB")

    session = _FakeSession(threshold=0.0)
    maybe_emit_model_cost_warn(session, "gpt-4o", action="session_start")
    after_first = session.event_snapshot()
    assert after_first, "expected first call to emit"

    maybe_emit_model_cost_warn(session, "gpt-4o", action="model_override")
    assert session.event_snapshot() == after_first, (
        "second call for same model class should not emit a new event"
    )


def test_maybe_emit_model_cost_warn_disabled_suppresses() -> None:
    """Tier 2: enabled=False suppresses all emission regardless of cost.

    Falsification: without the enabled check, users who set cost_warn.enabled:
    false would still receive warnings.
    """
    from reyn.llm.model_cost_rate import get_input_cost_per_1m_usd
    from reyn.runtime.model_cost_warn import maybe_emit_model_cost_warn

    if get_input_cost_per_1m_usd("gpt-4o") is None:
        pytest.skip("gpt-4o not in litellm pricing DB")

    session = _FakeSession(enabled=False, threshold=0.0)
    maybe_emit_model_cost_warn(session, "gpt-4o", action="session_start")
    assert not session.event_snapshot(), "expected no emit when disabled"


def test_maybe_emit_model_cost_warn_action_field_propagated() -> None:
    """Tier 2: the action kwarg reaches the event data field.

    Falsification: if the action parameter were hardcoded ('model_override'
    only), the session_start context would be misreported.
    """
    from reyn.llm.model_cost_rate import get_input_cost_per_1m_usd
    from reyn.runtime.model_cost_warn import maybe_emit_model_cost_warn

    if get_input_cost_per_1m_usd("gpt-4o") is None:
        pytest.skip("gpt-4o not in litellm pricing DB")

    session = _FakeSession(threshold=0.0)
    maybe_emit_model_cost_warn(session, "gpt-4o", action="session_start")
    events = session.event_snapshot()
    assert events, "expected an event to be emitted"
    _, evt_data = events[0]
    assert evt_data.get("action") == "session_start"
