"""Tier 2: #1212 D5 — per-(model, call-shape) response_format capability cache.

The cache lets the ``recorded_acompletion`` chokepoint skip a doomed
``response_format`` attempt after the provider rejected it once, instead of
re-paying the 400 round-trip on every call. The capability is call-shape
specific (``response_format`` alone vs combined with ``tools``), so the key is
``(model, has_tools)``.

Real cache + the real chokepoint; the only scripted seam is ``litellm.acompletion``
(the external provider boundary — the sanctioned pattern in
``test_cost_chokepoint_1190``), not a reyn collaborator mock.
"""
from __future__ import annotations

import asyncio

import litellm
import pytest

from reyn.llm import capability_cache as cc
from reyn.llm.llm import recorded_acompletion


@pytest.fixture(autouse=True)
def _reset_cache():
    cc.reset()
    yield
    cc.reset()


# ── cache module ────────────────────────────────────────────────────────────

def test_cache_records_and_reads_per_shape() -> None:
    """Tier 2: (model, has_tools) are independent keys; an un-probed shape reads None."""
    assert cc.response_format_supported("m", has_tools=False) is None
    cc.record_response_format_support("m", has_tools=False, supported=True)
    cc.record_response_format_support("m", has_tools=True, supported=False)
    assert cc.response_format_supported("m", has_tools=False) is True
    assert cc.response_format_supported("m", has_tools=True) is False
    assert cc.snapshot() == {("m", False): True, ("m", True): False}


def test_cache_reset_clears() -> None:
    """Tier 2: reset() clears recorded capabilities."""
    cc.record_response_format_support("m", has_tools=False, supported=True)
    cc.reset()
    assert cc.response_format_supported("m", has_tools=False) is None


# ── wiring into the recorded_acompletion chokepoint ─────────────────────────

class _Resp:
    """Minimal stand-in for a litellm response (recorder=None → not introspected)."""


def test_chokepoint_caches_rejection_and_skips_next(monkeypatch) -> None:
    """Tier 2: a response_format rejection is cached → the next call skips it.

    First call: rf attempted → rejected → fallback without rf (litellm sees
    [True, False]). The (model, has_tools=False) shape is now cached unsupported,
    so the SECOND call skips the doomed rf attempt entirely — litellm sees one
    more call, without rf ([True, False, False]) rather than re-probing.
    """
    calls: list[bool] = []

    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        has_rf = "response_format" in kw
        calls.append(has_rf)
        if has_rf:
            raise ValueError("response_format unsupported")
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake)

    async def _call():
        return await recorded_acompletion(
            model="m", messages=[{"role": "user", "content": "x"}], purpose="judge",
            recorder=None, response_format={"type": "json_object"},
            fallback_without_response_format=True,
        )

    asyncio.run(_call())
    assert calls == [True, False]
    assert cc.response_format_supported("m", has_tools=False) is False

    asyncio.run(_call())
    assert calls == [True, False, False], "2nd call must skip the doomed rf attempt"


def test_chokepoint_caches_success(monkeypatch) -> None:
    """Tier 2: a successful response_format call caches support=True."""
    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake)

    asyncio.run(recorded_acompletion(
        model="m2", messages=[{"role": "user", "content": "x"}], purpose="judge",
        recorder=None, response_format={"type": "json_object"},
        fallback_without_response_format=True,
    ))
    assert cc.response_format_supported("m2", has_tools=False) is True


def test_chokepoint_no_fallback_rejection_leaves_cache_untouched(monkeypatch) -> None:
    """Tier 2: fallback disabled + rf rejected → raises, cache untouched (semantics preserved)."""
    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        if "response_format" in kw:
            raise ValueError("rf unsupported")
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake)

    async def _call():
        return await recorded_acompletion(
            model="m3", messages=[{"role": "user", "content": "x"}], purpose="judge",
            recorder=None, response_format={"type": "json_object"},
            fallback_without_response_format=False,
        )

    with pytest.raises(ValueError):
        asyncio.run(_call())
    assert cc.response_format_supported("m3", has_tools=False) is None


def test_chokepoint_no_fallback_success_not_recorded(monkeypatch) -> None:
    """Tier 2: fallback disabled + rf SUCCEEDS → still not recorded.

    The invariant is "only the fallback-enabled path touches the cache". A
    non-fallback caller whose response_format call succeeds must NOT write the
    cache either (closes claim==content for the success path, not just rejection).
    """
    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake)

    asyncio.run(recorded_acompletion(
        model="m4", messages=[{"role": "user", "content": "x"}], purpose="judge",
        recorder=None, response_format={"type": "json_object"},
        fallback_without_response_format=False,
    ))
    assert cc.response_format_supported("m4", has_tools=False) is None
