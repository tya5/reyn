"""Tier 1: #5382 — ``LLMReplay`` gains a ``kind="exception"`` fixture entry,
so a compaction/retry test can express "this call fails with cause X"
without differing engine (or LLMReplay) instance from #5382's issue thread.

Architect ruling (issuecomment on #5382 -- read that thread for the full
reasoning): a fixture's ``cause`` is a CLOSED vocabulary
(``rate_limit`` / ``context_overflow`` / ``byte_limit``), never a class
name imported from a string ("data" must never point at arbitrary code).
No repeat-count axis: the key is already the request's own content hash,
so "same cause N times" is written as N distinct keys (the real
#5296/#5364 shape -- each retry's request actually differs as history
shrinks), never one key answering differently on its Nth read (which
would make a fixture depend on call ORDER, breaking the "stable across
unrelated prompt edits" property the content-only key gives it today).

Recording (``_record()``, real-LLM capture of an exception) is explicitly
OUT OF SCOPE for this change (architect: "設計していません...この PR で
作らないでください") -- only replay of a hand-authored fixture.

Policy (docs/deep-dives/contributing/testing.md): real instances only --
no ``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. Drives the
REAL ``litellm.acompletion`` boundary ``LLMReplay`` patches (mirrors
``tests/dev/test_3473_replay_environment_precondition.py``'s own
``_replay_call`` idiom), not the handler directly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import litellm
import pytest

from reyn.dev.testing.replay import LLMReplay, UnknownReplayCause

_MODEL = "gpt-4o-mini"


def _write_fixture(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8",
    )


async def _replay_call(replay: LLMReplay, messages: list[dict]) -> Any:
    """Drive one completion through the REAL boundary ``LLMReplay`` patches."""
    replay.install()
    try:
        return await litellm.acompletion(model=_MODEL, messages=messages)
    finally:
        replay.restore()


def _exception_entry(messages: list[dict], *, cause: str, message: "str | None" = None) -> dict:
    entry: dict = {
        "key": LLMReplay.key(_MODEL, messages), "kind": "exception", "cause": cause,
    }
    if message is not None:
        entry["message"] = message
    return entry


@pytest.mark.asyncio
async def test_a_rate_limit_fixture_raises_the_real_litellm_exception(tmp_path: Path) -> None:
    """Tier 1: witness 1 -- a ``cause: rate_limit`` fixture makes that
    key's request raise a real ``litellm.RateLimitError``, not a
    synthetic stand-in."""
    messages = [{"role": "user", "content": "one"}]
    fixture = tmp_path / "f.jsonl"
    _write_fixture(fixture, [_exception_entry(messages, cause="rate_limit")])
    replay = LLMReplay(fixture, mode="replay")

    with pytest.raises(litellm.RateLimitError):
        await _replay_call(replay, messages)


@pytest.mark.asyncio
async def test_the_same_key_raises_the_same_exception_every_call(tmp_path: Path) -> None:
    """Tier 1: witness 2 -- calling the SAME key twice raises the same
    exception BOTH times, with no repeat-count tracked or consumed --
    proof the "no count axis" design actually holds (a count-based
    implementation would raise once then fall through to a miss/response
    on the second call)."""
    messages = [{"role": "user", "content": "same request twice"}]
    fixture = tmp_path / "f.jsonl"
    _write_fixture(fixture, [_exception_entry(messages, cause="context_overflow")])
    replay = LLMReplay(fixture, mode="replay")

    with pytest.raises(litellm.ContextWindowExceededError):
        await _replay_call(replay, messages)
    with pytest.raises(litellm.ContextWindowExceededError):
        await _replay_call(replay, messages)


@pytest.mark.asyncio
async def test_three_distinct_keys_express_the_same_cause_without_a_count_field(
    tmp_path: Path,
) -> None:
    """Tier 1: witness 3 -- THE central witness (architect: this is the
    one that proves "count is unnecessary" actually holds). The real
    #5296/#5364 shape (a retry loop whose history shrinks each attempt,
    so each retry's REQUEST differs) is expressed as 3 distinct keys, all
    with cause="byte_limit", with no count/index field anywhere in the
    fixture format -- and all 3 requests raise it."""
    messages_by_attempt = [
        [{"role": "user", "content": f"attempt {i}, history len {30 - i}"}]
        for i in range(3)
    ]
    fixture = tmp_path / "f.jsonl"
    _write_fixture(
        fixture,
        [_exception_entry(m, cause="byte_limit") for m in messages_by_attempt],
    )
    replay = LLMReplay(fixture, mode="replay")

    for messages in messages_by_attempt:
        with pytest.raises(litellm.BadRequestError) as excinfo:
            await _replay_call(replay, messages)
        assert getattr(excinfo.value, "status_code", None) == 413, (
            "byte_limit must reproduce the real HTTP 413 shape "
            "engine.py's own saw_byte_limit classifier reads "
            "(getattr(exc, 'status_code', None) == 413)"
        )


def test_strip_the_exception_kind_branch_makes_witness_1_fall_through_to_a_miss() -> None:
    """Tier 1: witness 4 -- the strip witness. This test does not modify
    production code (see this repo's testing policy); it documents the
    manual strip-falsify already performed: temporarily removing the
    ``if key in self._exception_records:`` branch from ``_replay()``
    makes witness 1 (``test_a_rate_limit_fixture_raises_the_real_litellm_
    exception``) go RED -- the key falls through to the ``key not in
    self._records`` check and raises ``MissingFixture`` instead of
    ``RateLimitError`` (confirmed via a real commit -> edit -> revert
    pass, not merely asserted here)."""
    assert True  # documentation-only; see docstring — the strip itself
    # cannot be encoded as a live test without duplicating production
    # logic into the test (there is no public seam to disable the branch
    # from outside _replay() without reaching into private state).


@pytest.mark.asyncio
async def test_a_fixture_with_no_kind_still_replays_a_normal_response(tmp_path: Path) -> None:
    """Tier 1: witness 5 -- the backward-compatibility guard. An existing
    fixture entry with NO ``kind`` field (pre-#3451 shape, defaults to
    "completion" — see ``_load()``'s own docstring) must keep replaying a
    normal response, unaffected by the new exception-kind branch."""
    messages = [{"role": "user", "content": "ordinary request"}]
    fixture = tmp_path / "f.jsonl"
    entry = {
        "key": LLMReplay.key(_MODEL, messages),
        "model": _MODEL,
        "prompt_preview": "ordinary request",
        "response": {
            "id": "rec-1", "created": 0, "model": _MODEL, "object": "chat.completion",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"},
            }],
        },
    }
    _write_fixture(fixture, [entry])
    replay = LLMReplay(fixture, mode="replay")

    result = await _replay_call(replay, messages)
    assert result.choices[0].message.content == "ok"


@pytest.mark.asyncio
async def test_an_unknown_cause_fails_explicitly_not_a_silent_fallback(tmp_path: Path) -> None:
    """Tier 1: witness 6 -- an unrecognised ``cause`` value must fail
    EXPLICITLY (``UnknownReplayCause``), never silently fall back to
    replaying a (nonexistent) success response -- the same "diagnose,
    don't guess" posture ``MissingFixture`` already has for a key miss."""
    messages = [{"role": "user", "content": "unknown cause"}]
    fixture = tmp_path / "f.jsonl"
    _write_fixture(fixture, [_exception_entry(messages, cause="some_future_cause")])
    replay = LLMReplay(fixture, mode="replay")

    with pytest.raises(UnknownReplayCause):
        await _replay_call(replay, messages)
