"""Tier 2: #5597 — owner's own real machine: a compaction LLM call had
no per-request timeout at all, so an upstream provider that stopped
responding hung the call for 11+ minutes with zero reyn-side events —
`main`'s own router path already carries `safety.timeout.llm_call_seconds`
(60s default) + `num_retries`; compaction's call site
(`CompactionEngine._acompletion`, engine.py) passed neither.

Architect's own final ruling (issue #5597, verbatim): "新しい数を作らない。
routerの値をそのまま使う" — no new number, compaction inherits `main`'s
own already-chosen `safety.timeout.llm_call_seconds` value, resolved by
`recorded_acompletion` (the #1190 single funnel) itself — NOT by
touching `engine.py`'s own call site (unchanged, still passes neither
key; the funnel resolves for any caller that didn't set its own bound).
`num_retries` is untouched (already flows via the Router/ambient-context
source, unrelated to this fix). A NEW `chat.compaction.llm_call_seconds`
config knob (default `None` = inherit) exists purely so a LATER real p95
measurement can override the inherited value without a code change.

CLAUDE.md testing policy: no test writes a duration, either direction —
this file never sleeps and never asserts on wall-clock elapsed time; it
asserts on the RESOLVED bound value itself (what got passed to the real
call boundary), never on how long anything took.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.config.chat import SafetyConfig
from reyn.core.events.state_log import StateLog
from reyn.dev.testing.llm_stub import LLMStub
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle
from tests.runtime.test_5296_pr2_byte_reduction_same_turn_retry import _push


def _make_compaction_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
    t_max: int = 2_500, llm_call_seconds: "float | None" = None,
):
    """Mirrors `test_5296_pr2...`'s own `_make_spill_session` construction
    (real Session, `t_max` forcing a small `effective_trigger` so real
    content genuinely produces compaction candidates), with the ONE
    field this file's own scenario needs and that helper does not
    expose: `chat.compaction.llm_call_seconds`.

    Returns `(session, safety)` — `safety` is the SAME `SafetyConfig`
    instance passed into construction, so a test compares an assertion
    against a value IT supplied, never a private `session._safety`
    read-back (testing policy: no private-state assertion)."""
    monkeypatch.chdir(tmp_path)
    import reyn.llm.model_budget as _mb
    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: t_max)
    cfg = CompactionConfig(
        body_token_cap=1500, use_chars4_estimate=True, section_caps_spec_tokens=0,
        llm_call_seconds=llm_call_seconds,
    )
    safety = SafetyConfig()
    session = make_session(
        agent_name="t5597", agent_role="", output_language="en",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        compaction_config=cfg,
        safety=safety,
        multimodal_config=MultimodalConfig(),
        snapshot_path=tmp_path / ".reyn" / "agents" / "t5597" / "state" / "snapshot.json",
    )
    for i in range(30):
        _push(session, "user", f"filler turn {i} " * 40)
    return session, safety


def _timeout_from_events(events, purpose: str = "compaction") -> "float | None":
    requests = [
        e for e in events if e.type == "llm_request" and e.data.get("purpose") == purpose
    ]
    assert requests, f"sanity: at least one llm_request(purpose={purpose!r}) must exist"
    return requests[-1].data["params"].get("timeout")


def test_compaction_call_inherits_mains_own_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5597 accept — a real compaction call (via
    `CompactionController.force_compact_now()`, the SAME real path
    #5498's own "Path 1" already establishes) resolves `timeout` to
    EXACTLY `safety.timeout.llm_call_seconds` — never a hardcoded `60`
    literal in this test (lead-coder's own instruction), always compared
    against the session's own real config value."""
    session, safety = _make_compaction_session(tmp_path, monkeypatch)
    events = collect_events(session)
    stub = LLMStub()
    stub.install()
    try:
        asyncio.run(session._compaction_controller.force_compact_now())
        asyncio.run(settle(session))
    finally:
        stub.restore()

    resolved_timeout = _timeout_from_events(events)
    assert resolved_timeout == safety.timeout.llm_call_seconds, (
        f"compaction's own resolved timeout must equal main's own "
        f"safety.timeout.llm_call_seconds ({safety.timeout.llm_call_seconds}) "
        f"— got {resolved_timeout!r}"
    )


def test_compaction_llm_call_seconds_override_wins_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5597 accept sibling — `chat.compaction.llm_call_seconds`,
    when an operator sets it, overrides the inherited `safety.timeout.
    llm_call_seconds` value for compaction calls specifically — never
    the other way around, and never affecting any other purpose."""
    override_seconds = 12345.0  # deliberately NOT the safety default — a real, distinct value
    session, safety = _make_compaction_session(
        tmp_path, monkeypatch, llm_call_seconds=override_seconds,
    )
    assert override_seconds != safety.timeout.llm_call_seconds, (
        "sanity: the override must genuinely differ from the inherited "
        "default, else this test cannot distinguish the two"
    )

    events = collect_events(session)
    stub = LLMStub()
    stub.install()
    try:
        asyncio.run(session._compaction_controller.force_compact_now())
        asyncio.run(settle(session))
    finally:
        stub.restore()

    resolved_timeout = _timeout_from_events(events)
    assert resolved_timeout == override_seconds, (
        f"expected the operator override ({override_seconds}) to win — "
        f"got {resolved_timeout!r}"
    )


def test_explicit_caller_timeout_is_never_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #5597 deny — the kernel path's own explicit `timeout`
    (threaded via the LLMCallRecorder, #2210's own contract) is never
    overwritten by the funnel's own resolution — regression-impossible
    by construction (`if "timeout" not in base_kwargs` in
    `recorded_acompletion`), verified directly at the funnel's own
    public entry point."""
    from reyn.dev.testing.llm_stub import LLMStub as _Stub
    from reyn.llm.llm import recorded_acompletion

    explicit_timeout = 7.5
    stub = _Stub()
    stub.install()
    captured: "dict[str, object]" = {}
    try:
        import litellm
        _orig = litellm.acompletion

        async def _spy(*args, **kwargs):
            captured.update(kwargs)
            return await _orig(*args, **kwargs)

        monkeypatch.setattr(litellm, "acompletion", _spy)
        asyncio.run(
            recorded_acompletion(
                model="openai/test-standard-model",
                messages=[{"role": "user", "content": "hi"}],
                purpose="compaction",
                model_class=None,
                extra_kwargs={"timeout": explicit_timeout},
            ),
        )
    finally:
        stub.restore()

    assert captured.get("timeout") == explicit_timeout, (
        f"an explicit caller-supplied timeout must reach litellm.acompletion "
        f"verbatim, never overridden by the funnel's own ambient resolution "
        f"— got {captured.get('timeout')!r}"
    )


def test_funnel_resolved_timeout_genuinely_reaches_litellm_acompletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5597 accept — reachability, not just "the event says so"
    ("applied ≠ reached", architect's own #5620 caution reused here): the
    ACTUAL kwargs `litellm.acompletion` receives (spied at the real
    boundary `LLMStub` itself patches) carry the resolved `timeout` —
    not merely the `llm_request` audit-event's own `params` field, which
    could in principle diverge from what was truly forwarded."""
    session, safety = _make_compaction_session(tmp_path, monkeypatch)
    stub = LLMStub()
    stub.install()
    captured: "dict[str, object]" = {}
    try:
        import litellm
        _orig = litellm.acompletion

        async def _spy(*args, **kwargs):
            captured.update(kwargs)
            return await _orig(*args, **kwargs)

        monkeypatch.setattr(litellm, "acompletion", _spy)
        asyncio.run(session._compaction_controller.force_compact_now())
        asyncio.run(settle(session))
    finally:
        stub.restore()

    assert captured.get("timeout") == safety.timeout.llm_call_seconds, (
        f"the resolved timeout must genuinely reach litellm.acompletion's "
        f"own real kwargs, not just the audit event — got "
        f"{captured.get('timeout')!r}, expected "
        f"{safety.timeout.llm_call_seconds!r}"
    )


def test_a_real_timeout_exception_classifies_retryable_not_overflow() -> None:
    """Tier 1: reyn's own classification contract (`classify_llm_
    failure`, #5593) — a real `litellm.Timeout` (the exact exception
    type a hung upstream provider now produces instead of blocking
    forever, since #5597 gives the compaction call a per-request
    timeout to fire in the first place) classifies RETRYABLE, never
    OVERFLOW. Relevant to #5597 because every call site that gates
    entry to the shrink ladder excludes FATAL/RETRYABLE before ever
    wrapping an exception as a shrinkable overflow — but that routing
    is a separate, already-tested contract of its own (#5577/#5593/
    #5622's own suites); this test pins only the classification
    itself, directly."""
    import litellm

    from reyn.services.compaction.engine import LLMFailureClass, classify_llm_failure

    exc = litellm.Timeout(message="Request timed out.", model="gpt-5", llm_provider="openai")
    assert classify_llm_failure(exc) is LLMFailureClass.RETRYABLE, (
        f"a real litellm.Timeout must classify RETRYABLE — got "
        f"{classify_llm_failure(exc)!r}"
    )
