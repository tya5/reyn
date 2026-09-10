"""Tier 1: LLMReplay's #6070 repair-by-replay path — a record-mode call whose
group_signature (model + tool_choice + per-message digests, EXCLUDING
tools — the existing #3634 grouping rule) matches an entry ALREADY on disk
reuses that entry's own response instead of reaching the real LLM.

Real LLMReplay throughout, `monkeypatch.setattr("litellm.acompletion", ...,
raising=False)` BEFORE `install()` — the same technique
`test_replay_unconsumed_5283.py`'s own real-instance witness and
`test_fp0063_arc_witness.py`'s own GENERATE path both use. The witness that
matters is negative: the fake set up to prove reuse RAISES if ever called,
so a passing test proves the real call was skipped, not merely that SOME
response came back.
"""
from __future__ import annotations

import asyncio
import json

from reyn.dev.testing.replay import LLMReplay

_MODEL = "openai/gemini-2.5-flash-lite"
_MESSAGES = [{"role": "user", "content": "hello"}]
_TOOLS_OLD = [
    {"type": "function", "function": {"name": "exec", "description": "old schema", "parameters": {}}}
]
_TOOLS_NEW = [
    {
        "type": "function",
        "function": {
            "name": "exec", "description": "new schema, cmd field added",
            "parameters": {"properties": {"cmd": {"type": "string"}}},
        },
    }
]


class _FakeResponse:
    """Mirrors test_replay_unconsumed_5283.py's own _FakeResponse shape —
    the minimum a real litellm.ModelResponse.model_dump() carries that
    LLMReplay's own record/replay round-trip reads back."""

    def __init__(self, content: str) -> None:
        self._content = content

    def model_dump(self):
        return {
            "id": "fake",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self._content, "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "model": _MODEL,
            "object": "chat.completion",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


def _seed_old_fixture(fixture_path, monkeypatch, content: str) -> str:
    """Record ONE completion entry against _TOOLS_OLD via a real LLMReplay
    in mode="record" (never hand-written JSON — the key/key_components must
    be exactly what the real code produces). Returns the recorded key."""

    async def _fake_acompletion(**kwargs):
        return _FakeResponse(content)

    async def _record():
        monkeypatch.setattr("litellm.acompletion", _fake_acompletion, raising=False)
        replay = LLMReplay(fixture_path, mode="record")
        replay.install()
        try:
            import litellm

            await litellm.acompletion(
                model=_MODEL, messages=_MESSAGES, tools=_TOOLS_OLD, tool_choice="auto",
            )
        finally:
            replay.restore()
            replay.flush()

    asyncio.run(_record())
    old_key = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_OLD, tool_choice="auto")
    assert old_key in fixture_path.read_text(encoding="utf-8"), (
        "seeding fixture drifted -- the old entry's own key is not on disk"
    )
    return old_key


def test_a_pure_schema_rekey_reuses_the_old_response_without_calling_the_real_llm(
    tmp_path, monkeypatch,
):
    """Tier 1: the load-bearing witness. Same model/messages/tool_choice,
    ONLY tools differs (a schema drift, #6065/#5838 stage 6's own shape) —
    #6070's repair path must serve the OLD content and must NEVER reach
    the real acompletion for this call."""
    fixture_path = tmp_path / "fixture.jsonl"
    old_key = _seed_old_fixture(fixture_path, monkeypatch, content="the old, already-recorded answer")

    async def _must_not_be_called(**kwargs):
        raise AssertionError(
            "the real LLM was called -- #6070's repair-by-replay path did "
            "not reuse the prior generation's response"
        )

    async def _rerecord():
        monkeypatch.setattr("litellm.acompletion", _must_not_be_called, raising=False)
        replay = LLMReplay(fixture_path, mode="record")
        replay.install()
        try:
            import litellm

            return await litellm.acompletion(
                model=_MODEL, messages=_MESSAGES, tools=_TOOLS_NEW, tool_choice="auto",
            )
        finally:
            replay.restore()
            replay.flush()

    response = asyncio.run(_rerecord())
    assert response.choices[0].message.content == "the old, already-recorded answer", (
        "the repaired entry's content must be the REPLAYED prior answer, "
        "never a freshly fabricated one"
    )

    new_key = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_NEW, tool_choice="auto")
    lines = [json.loads(line) for line in fixture_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    new_entry = next(e for e in lines if e.get("key") == new_key)
    assert new_entry.get("reused_from_key") == old_key, (
        "condition 2 of lead-coder's #6070 ruling -- the fixture itself must "
        "show this entry was re-keyed, not freshly recorded"
    )
    assert not any(e.get("key") == old_key for e in lines), (
        "the stale prior-generation entry must be replaced, not left stacked "
        "alongside the new one (#3634's own existing flush() dedup)"
    )


def test_a_genuinely_new_call_shape_still_reaches_the_real_llm(tmp_path, monkeypatch):
    """Tier 1: the false-accept-side witness required alongside the test
    above — a detector^Wrepair path that reuses EVERY call unconditionally
    would ALSO pass the reuse test. Different messages (not just different
    tools) must NOT match any prior generation, so the real call still
    happens."""
    fixture_path = tmp_path / "fixture.jsonl"
    _seed_old_fixture(fixture_path, monkeypatch, content="the old answer")

    called = {"hit": False}

    async def _fake_acompletion(**kwargs):
        called["hit"] = True
        return _FakeResponse("a genuinely fresh answer")

    async def _record_new_conversation():
        monkeypatch.setattr("litellm.acompletion", _fake_acompletion, raising=False)
        replay = LLMReplay(fixture_path, mode="record")
        replay.install()
        try:
            import litellm

            return await litellm.acompletion(
                model=_MODEL,
                messages=[{"role": "user", "content": "a completely different prompt"}],
                tools=_TOOLS_NEW, tool_choice="auto",
            )
        finally:
            replay.restore()
            replay.flush()

    response = asyncio.run(_record_new_conversation())
    assert called["hit"], "a genuinely new conversation must still reach the real LLM"
    assert response.model_dump()["choices"][0]["message"]["content"] == "a genuinely fresh answer"


def test_a_freshly_recorded_entry_carries_no_reused_from_key_field(tmp_path, monkeypatch):
    """Tier 1: absence-means-normal (this repo's own established convention)
    — a call with NO prior generation to reuse records a plain entry, with
    no `reused_from_key` field at all, not e.g. `null`."""
    fixture_path = tmp_path / "fixture.jsonl"

    async def _fake_acompletion(**kwargs):
        return _FakeResponse("first ever recording")

    async def _record():
        monkeypatch.setattr("litellm.acompletion", _fake_acompletion, raising=False)
        replay = LLMReplay(fixture_path, mode="record")
        replay.install()
        try:
            import litellm

            await litellm.acompletion(
                model=_MODEL, messages=_MESSAGES, tools=_TOOLS_OLD, tool_choice="auto",
            )
        finally:
            replay.restore()
            replay.flush()

    asyncio.run(_record())
    lines = [json.loads(line) for line in fixture_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    new_key = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_OLD, tool_choice="auto")
    entry = next(e for e in lines if e.get("key") == new_key)
    assert "reused_from_key" not in entry


def test_only_the_on_disk_fixture_at_construction_is_a_repair_source(tmp_path, monkeypatch):
    """Tier 1: a call this SAME session already answered must never become a
    repair source for a LATER call in the same run — only the fixture as
    it stood BEFORE this LLMReplay instance was constructed. Two distinct
    conversations recorded fresh in one session, both real calls, must
    both actually reach the fake (neither serves the other)."""
    fixture_path = tmp_path / "fixture.jsonl"
    call_count = {"n": 0}

    async def _fake_acompletion(**kwargs):
        call_count["n"] += 1
        return _FakeResponse(f"answer #{call_count['n']}")

    async def _record_two_fresh_calls():
        monkeypatch.setattr("litellm.acompletion", _fake_acompletion, raising=False)
        replay = LLMReplay(fixture_path, mode="record")
        replay.install()
        try:
            import litellm

            await litellm.acompletion(
                model=_MODEL, messages=[{"role": "user", "content": "first"}],
                tools=_TOOLS_OLD, tool_choice="auto",
            )
            await litellm.acompletion(
                model=_MODEL, messages=[{"role": "user", "content": "second"}],
                tools=_TOOLS_OLD, tool_choice="auto",
            )
        finally:
            replay.restore()
            replay.flush()

    asyncio.run(_record_two_fresh_calls())
    assert call_count["n"] == 2, (
        "both distinct calls in one fresh session must reach the real LLM -- "
        "neither has a prior on-disk generation to repair from"
    )
