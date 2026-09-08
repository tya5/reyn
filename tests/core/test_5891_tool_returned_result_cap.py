"""Tier 1/2: #5891 — `tool_returned.data["result"]` had no size cap:
measured on reyn-self, two `exec` results were ~2.79MB and ~5.94MB, the
raw tool-call return value written straight into ONE audit-event line.

Architect's ruling (issue #5891, verbatim shape): mirror `backend.py`'s
existing `_persist_llm_request_error` (#4975) skeleton — `<field>_length`
(here `result_bytes`) and a content hash (`result_sha256`) ALWAYS added,
computed over the FULL untruncated body; `result` itself replaced with an
excerpt, and `result_truncated` added, ONLY when the body exceeds the cap
(`tool_result_max_chars`). `content_ref` is deliberately NOT added this
PR (deferred past #5896 stage before its own stage ①) — only a fixed-
string `content_ref_unavailable` reason field, always present, so a
reader cannot mistake "no ref wired yet" for "no full body ever
existed".

🔴 Ordering requirement (architect, load-bearing): the excerpt/hash MUST
be built from the payload `_redact_content_fields` (`core/dispatch/
dispatcher.py`) already ran over — never from a pre-redaction payload,
or a per-tool content declaration (`ask_user`'s `answer`, governed by
`user_input_include_text`) would be re-exposed by the cap. `test_
excerpt_reflects_the_post_redaction_payload_dispatch_tool_hands_backend`
below drives the REAL `dispatch_tool` (not a stand-in for it) to prove
this end to end.

Real `LocalEventBackend` + a minimal real `EventStoreLike` (mirrors
`tests/core/test_4975_provider_body_lattice_meet.py`'s own
`_RecordingStore`) throughout — no mocks.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from reyn.core.dispatch.content_declarations import _TOOL_CONTENT_FIELDS, declare_content_fields
from reyn.core.dispatch.dispatcher import DispatchContext, dispatch_tool
from reyn.core.events.backend import LocalEventBackend
from reyn.schemas.models import Event


class _RecordingStore:
    def __init__(self) -> None:
        self.written: "list[Event]" = []

    def write(self, event: Event) -> None:
        self.written.append(event)


def _returned_event(result) -> Event:
    return Event(
        type="tool_returned",
        data={
            "caller_kind": "router",
            "caller_id": "test_agent",
            "tool": "exec",
            "chain_id": "c1",
            "call_id": "call1",
            "args_hash": "deadbeef",
            "result": result,
        },
    )


def test_large_result_is_capped_and_bounds_the_written_event_size():
    """Tier 1: acceptance① — a 5MB result is excerpted to the configured
    cap, `result_truncated` is set, and the FULL body's true byte length
    and sha256 survive alongside the excerpt (computed BEFORE
    truncation, over the whole body — #5891's own "lets a later reader
    correlate an excerpt with the full body once a ref exists" reason).
    Strip-falsify: removing the cap check in `_persist_tool_returned`
    (so `result` is never replaced) makes the final bound assertion
    fail — the written line grows to ~5MB instead of staying near the
    4000-char cap."""
    big = "x" * (5 * 1024 * 1024)
    store = _RecordingStore()
    backend = LocalEventBackend(store)  # tool_result_max_chars default: 4000

    backend.write(_returned_event(big))

    data = store.written[0].data
    assert data["result_truncated"] is True
    assert data["result"] == big[:4000]
    assert data["result_bytes"] == len(big.encode("utf-8"))
    assert data["result_sha256"] == hashlib.sha256(big.encode("utf-8")).hexdigest()

    # The full written line must be bounded near the cap, not near the
    # original 5MB body — the excerpt plus a generous constant for every
    # other field on this event (caller_kind/tool/chain_id/call_id/
    # args_hash/result_bytes/result_sha256/result_truncated/
    # content_ref_unavailable), never anything proportional to `big`
    # (`original_line_bytes` below is the untruncated body's own
    # serialized size — the bound this assertion falsifies against, not
    # a re-derived literal).
    original_line_bytes = len(big.encode("utf-8"))
    line_bytes = len(store.written[0].model_dump_json().encode("utf-8"))
    assert line_bytes < original_line_bytes // 100, (
        f"tool_returned line was {line_bytes} bytes, "
        f"not meaningfully smaller than the original body's {original_line_bytes} "
        "-- the cap did not bound it"
    )


def test_short_result_is_not_truncated_and_a_long_one_in_the_same_run_is():
    """Tier 1: acceptance② — the negative half (a genuinely short result,
    under the cap, is written whole with NO `result_truncated` key) and
    the positive half (a result over the cap in the SAME test run DOES
    get the marker) are asserted together, so a stub that always/never
    sets the marker cannot pass by only exercising one direction."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, tool_result_max_chars=10)

    backend.write(_returned_event("short"))
    backend.write(_returned_event("this text is over ten characters long"))

    short_data = store.written[0].data
    long_data = store.written[1].data
    assert short_data["result"] == "short"
    assert "result_truncated" not in short_data
    assert long_data["result"] == "this text "
    assert long_data["result_truncated"] is True


def test_a_dict_result_under_the_cap_is_kept_as_the_original_object():
    """Tier 1: a non-str result under the cap is written back UNCHANGED
    (the original dict, not its serialized-string form) — only
    `result_bytes`/`result_sha256` (over the deterministic
    `json.dumps(..., sort_keys=True)` serialization) are derived from
    it. `sort_keys=True` is what makes the hash independent of the
    dict's own insertion order."""
    store = _RecordingStore()
    backend = LocalEventBackend(store)
    result = {"b": 2, "a": 1}

    backend.write(_returned_event(result))

    data = store.written[0].data
    assert data["result"] == result
    serialized = json.dumps(result, default=str, sort_keys=True)
    assert data["result_bytes"] == len(serialized.encode("utf-8"))
    assert data["result_sha256"] == hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    assert "result_truncated" not in data


def test_content_ref_unavailable_is_always_present_with_a_fixed_reason():
    """Tier 1: #5891 adds the size cap only — `content_ref` (the actual
    reference mechanism) is #5896's, not this PR's. `content_ref_
    unavailable` names that explicitly, unconditionally, on both a
    truncated and an untruncated event, so a reader cannot mistake
    silence for "the full body never existed"."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, tool_result_max_chars=10)

    backend.write(_returned_event("short"))
    backend.write(_returned_event("this text is over ten characters long"))

    for data in (store.written[0].data, store.written[1].data):
        assert data["content_ref_unavailable"] == (
            "not yet wired (#5891, stage before #5896 stage ① ref-linking)"
        )


def test_a_missing_result_field_records_no_length_or_hash_either():
    """Tier 1: mirrors #4975's own `test_a_missing_provider_body_records_
    no_length_either` — "there was none" must stay distinguishable from
    "there was one but it was capped". A tool_returned event with no
    `result` key at all must not fabricate `result_bytes`/`result_sha256`,
    though `content_ref_unavailable` is still unconditional."""
    store = _RecordingStore()
    backend = LocalEventBackend(store)

    backend.write(Event(
        type="tool_returned",
        data={"caller_kind": "router", "caller_id": "a", "tool": "t",
              "chain_id": None, "call_id": None, "args_hash": "h"},
    ))

    data = store.written[0].data
    assert "result" not in data
    assert "result_bytes" not in data
    assert "result_sha256" not in data
    assert "result_truncated" not in data
    assert "content_ref_unavailable" in data


class FakeEventEmitter:
    """Mirrors `tests/core/test_4666_tool_content_declarations.py`'s own
    fixture, plus routing straight into a REAL `LocalEventBackend` — the
    same `EventLog.emit() -> backend.write(event)` handoff production
    uses (`core/events/backend.py`'s own module docstring), so this test
    exercises the actual ordering guarantee end to end rather than
    re-asserting `_redact_content_fields`'s own behavior in isolation."""

    def __init__(self, backend: LocalEventBackend) -> None:
        self._backend = backend
        self.written: "list[Event]" = []

    def emit(self, event_type: str, **data) -> None:
        event = Event(type=event_type, data=data)
        self._backend.write(event)
        self.written.append(event)

    def find(self, event_type: str) -> dict:
        return next(e.data for e in self.written if e.type == event_type)


def _reset_probe_tool_declaration() -> None:
    _TOOL_CONTENT_FIELDS.pop("probe_tool", None)


async def _handler_returns_sensitive_answer(args: dict) -> dict:
    return {"question": args.get("question", ""), "answer": "SENSITIVE_USER_TEXT" * 500}


def test_excerpt_reflects_the_post_redaction_payload_dispatch_tool_hands_backend():
    """Tier 2: 🔴 the ordering requirement, driven through the REAL
    `dispatch_tool` (`core/dispatch/dispatcher.py`) -- not a stand-in for
    it. `probe_tool` declares its `answer` field as "user" content
    (`content_declarations`, the same mechanism `ask_user` uses);
    `dispatch_tool` drops it from `result` via `_redact_content_fields`
    BEFORE `ctx.events.emit("tool_returned", ..., result=...)` ever
    runs -- so `LocalEventBackend._persist_tool_returned` (downstream of
    that emit, via the store this test's `FakeEventEmitter` writes
    through) never even sees `answer`, and neither the excerpt nor the
    sha/bytes it derives can leak it.

    This is the GREEN half of the strip-falsify pair described in
    `_persist_tool_returned`'s own docstring: making `dispatch_tool`
    emit the pre-redaction `result` instead (bypassing
    `_redact_content_fields`) is what turns this assertion red -- that
    edit was made and reverted in-file to verify this test actually
    bites (CLAUDE.md: `git checkout`/`stash`/`restore` forbidden for
    this)."""
    async def main():
        _reset_probe_tool_declaration()
        declare_content_fields("probe_tool", {"answer": "user"})
        try:
            store = _RecordingStore()
            backend = LocalEventBackend(store, user_input_include_text=False)
            events = FakeEventEmitter(backend)
            catalog = {
                "probe_tool": {
                    "function": {
                        "name": "probe_tool",
                        "description": "test-only tool",
                        "parameters": {
                            "type": "object",
                            "properties": {"question": {"type": "string"}},
                        },
                    },
                },
            }
            ctx = DispatchContext(
                caller_kind="router",
                caller_id="test_agent",
                chain_id="c1",
                tool_catalog=catalog,
                events=events,
                contextual=None,
                tool_call_id=None,  # #5891 (c): required field, this test doesn't need one
                user_input_include_text=False,
            )

            await dispatch_tool(
                name="probe_tool", args={"question": "q"},
                ctx=ctx, invoker=_handler_returns_sensitive_answer,
            )

            # `store.written` holds BOTH `tool_called` and `tool_returned`
            # (dispatch_tool emits the former first) -- find the one
            # `_persist_tool_returned` actually touched, never assume
            # position.
            persisted = next(
                e.data for e in store.written if e.type == "tool_returned"
            )
            assert "answer" not in persisted.get("result", {})
            # The sensitive text must not have leaked anywhere on the
            # persisted record -- not in the excerpt, not incidentally
            # serialized in some other field.
            assert "SENSITIVE_USER_TEXT" not in json.dumps(persisted)
        finally:
            _reset_probe_tool_declaration()
    asyncio.run(main())
