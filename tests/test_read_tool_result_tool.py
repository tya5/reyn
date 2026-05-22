"""Tier 2: read_tool_result tool — companion to #385 PoC preview-driven
tool returns.

Pins the contract that LLM-callable tool ``read_tool_result(path=...)``:

1. Returns ``status="ok"`` + ``content`` when the path is valid and the
   file exists inside ``.reyn/tool-results/``.
2. Returns ``status="not_found"`` when the file was deleted (= user
   manually cleaned up under ``.reyn/tool-results/``).
3. Returns ``status="error"`` with a PermissionError-derived message
   when the path tries to escape the workspace boundary (= path
   traversal / path-ref injection).
4. Truncates at ``max_bytes`` with a clear ``truncated: True`` signal
   so the LLM can decide to re-call with a higher cap.
5. Surfaces a structured error (= ``status="error"``) when the session
   has no ``MediaStore`` configured rather than crashing — keeps the
   PoC degrade-safe for sessions outside the multimodal path.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.tools.read_tool_result import _handle
from reyn.tools.types import (
    PhaseCallerState,
    RouterCallerState,
    ToolContext,
)
from reyn.workspace.media_store import MediaStore, MediaStoreConfig


class _StubEvents:
    """Minimal stand-in for the events log.

    Captures emit calls so #385 β sub-task 2 tests can assert on the
    ``tool_result_read`` observability event payload. Existing tests
    that don't read the log are unaffected (= same constructor shape,
    extra capture list is opt-in).
    """
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []
        self.subscribers: list = []

    def emit(self, name: str, **kwargs) -> None:
        self.emitted.append((name, kwargs))


def _populate_tool_result(
    tmp_path: Path, content: str = "hello\nworld",
) -> tuple[MediaStore, str]:
    """Build a MediaStore, write a tool result, return (store, path-ref-str)."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    block = store.save_tool_result(
        content, mime_type="text/plain",
        chain_id="abc123", tool="web_fetch", seq=1,
    )
    return store, block["path"]


def _ctx_with_media_store(
    media_store: MediaStore | None, *, events: _StubEvents | None = None,
) -> ToolContext:
    """Build a minimal router-caller ToolContext whose router_state
    factory hands back an OpContext carrying ``media_store``.

    Optional ``events`` lets callers inject a shared ``_StubEvents``
    instance — used by #385 β sub-task 2 tests to assert on emitted
    ``tool_result_read`` payloads. When omitted, a fresh stub is
    constructed so existing tests stay unaffected.
    """
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    events_log = events or _StubEvents()

    def _factory() -> OpContext:
        return OpContext(
            workspace=None,
            events=events_log,
            permission_decl=PermissionDecl(),
            permission_resolver=None,
            skill_name="",
            subscribers=[],
            media_store=media_store,
        )

    return ToolContext(
        events=events_log,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(op_context_factory=_factory),
        phase_state=None,
    )


# ── happy path ─────────────────────────────────────────────────────────


def test_read_tool_result_returns_full_content_when_below_cap(tmp_path):
    """Tier 2: small file under default max_bytes returns full content
    with ``truncated=False``.
    """
    store, path_ref = _populate_tool_result(tmp_path, "hello\nworld\n")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref}, ctx))

    assert result["status"] == "ok"
    assert result["path"] == path_ref
    assert result["content"] == "hello\nworld\n"
    assert result["truncated"] is False
    assert result["total_bytes"] == len("hello\nworld\n".encode("utf-8"))


def test_read_tool_result_truncates_when_above_max_bytes(tmp_path):
    """Tier 2: content larger than ``max_bytes`` truncates and surfaces
    ``truncated=True`` + ``total_bytes`` so the LLM can re-call with a
    higher cap.
    """
    big = "a" * 5000
    store, path_ref = _populate_tool_result(tmp_path, big)
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref, "max_bytes": 1000}, ctx))

    assert result["status"] == "ok"
    assert result["truncated"] is True
    assert result["max_bytes"] == 1000
    assert result["total_bytes"] == 5000
    assert len(result["content"]) == 1000


# ── error / edge cases ─────────────────────────────────────────────────


def test_read_tool_result_missing_path_arg_returns_error(tmp_path):
    """Tier 2: empty / missing ``path`` AND ``resource_uri`` surfaces a
    structured error without touching the filesystem.

    The error message names both identifiers — the schema is
    "exactly one of path or resource_uri", and the LLM's correction path
    needs to know which alternative is acceptable.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({}, ctx))

    assert result["status"] == "error"
    assert "path" in result["error"]
    assert "resource_uri" in result["error"]


def test_read_tool_result_outside_tool_results_dir_rejected(tmp_path):
    """Tier 2: a path that escapes ``.reyn/tool-results/`` (e.g. via
    ``..``) is rejected with an error rather than read.

    Defends against an adversarial / malformed path-ref smuggling in a
    file outside the workspace media boundary.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    ctx = _ctx_with_media_store(store)

    # A path that escapes via .. — read_tool_result on MediaStore raises
    # PermissionError, the tool handler catches it and surfaces the
    # message under ``error``.
    result = asyncio.run(
        _handle({"path": "../../../etc/passwd"}, ctx),
    )

    assert result["status"] == "error"
    assert "outside" in result["error"]


def test_read_tool_result_missing_file_returns_not_found(tmp_path):
    """Tier 2: a path inside ``tool_results_dir`` whose file no longer
    exists (= user deleted via ``rm``) surfaces ``status=not_found``
    rather than crashing.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    # Create the directory so the path validation succeeds, then point
    # at a file that doesn't exist inside it.
    store.tool_results_dir.mkdir(parents=True, exist_ok=True)
    fake_rel = str(
        (store.tool_results_dir / "deleted-file.txt").relative_to(tmp_path)
    )
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": fake_rel}, ctx))

    assert result["status"] == "not_found"
    assert result["path"] == fake_rel


def test_read_tool_result_without_media_store_degrades_with_error(tmp_path):
    """Tier 2: when the session has no MediaStore (= legacy / non-
    multimodal path), the tool returns a structured error rather than
    crashing — keeps the PoC degrade-safe.
    """
    ctx = _ctx_with_media_store(media_store=None)

    result = asyncio.run(
        _handle({"path": ".reyn/tool-results/anything.txt"}, ctx),
    )

    assert result["status"] == "error"
    assert "MediaStore" in result["error"]


# ── offset / limit line-slice (= PR #409 4-surface symmetry adoption, Q7) ──


def test_read_tool_result_offset_only_skips_leading_lines(tmp_path):
    """Tier 2: ``offset=N`` starts at line N (0-indexed), reads through
    end-of-body. Mirrors ``read_file`` / ``reyn_src_read`` /
    ``read_memory_body`` semantics introduced in PR #409.
    """
    store, path_ref = _populate_tool_result(
        tmp_path, "L0\nL1\nL2\nL3\nL4\n",
    )
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref, "offset": 2}, ctx))

    assert result["status"] == "ok"
    assert result["content"] == "L2\nL3\nL4\n"


def test_read_tool_result_limit_only_takes_first_n(tmp_path):
    """Tier 2: ``limit=N`` without ``offset`` takes the first N lines."""
    store, path_ref = _populate_tool_result(
        tmp_path, "L0\nL1\nL2\nL3\nL4\n",
    )
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref, "limit": 2}, ctx))

    assert result["status"] == "ok"
    assert result["content"] == "L0\nL1\n"


def test_read_tool_result_offset_and_limit_window(tmp_path):
    """Tier 2: combining ``offset`` + ``limit`` returns the
    ``[offset, offset+limit)`` line window.
    """
    store, path_ref = _populate_tool_result(
        tmp_path, "L0\nL1\nL2\nL3\nL4\n",
    )
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle({"path": path_ref, "offset": 1, "limit": 2}, ctx),
    )

    assert result["status"] == "ok"
    assert result["content"] == "L1\nL2\n"


def test_read_tool_result_offset_past_eof_returns_empty(tmp_path):
    """Tier 2: ``offset`` past the body's last line returns empty
    content — never an error. Matches the past-EOF semantic of the
    three sister read tools so the LLM detects out-of-range without a
    structured failure path.
    """
    store, path_ref = _populate_tool_result(tmp_path, "L0\nL1\nL2\n")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref, "offset": 99}, ctx))

    assert result["status"] == "ok"
    assert result["content"] == ""


def test_read_tool_result_slice_then_max_bytes_compose(tmp_path):
    """Tier 2: ``offset`` / ``limit`` (= line slice) are applied BEFORE
    ``max_bytes`` (= byte cap) — the two axes compose. Verifies the
    slice happens against the full body, then the resulting sliced
    text is byte-capped if it still exceeds ``max_bytes``.

    Scenario: 100 lines of 20-char content (= ~2000 bytes). offset=10,
    limit=50 → sliced ~1000 bytes. max_bytes=300 → final 300 bytes of
    the sliced window, ``truncated=True`` on the sliced size.
    """
    lines = [f"line {i:>3} content" for i in range(100)]  # 16-char each
    body = "\n".join(lines) + "\n"
    store, path_ref = _populate_tool_result(tmp_path, body)
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle(
            {"path": path_ref, "offset": 10, "limit": 50, "max_bytes": 300},
            ctx,
        ),
    )

    assert result["status"] == "ok"
    # max_bytes hit, surfaces truncated=True.
    assert result["truncated"] is True
    assert result["max_bytes"] == 300
    # content starts at offset=10 (= "line  10 content"), not the file head.
    assert result["content"].startswith("line  10 content")
    # content is exactly 300 bytes (= max_bytes cap on the sliced window).
    assert len(result["content"].encode("utf-8")) == 300


def test_read_tool_result_no_slice_args_is_unchanged_behaviour(tmp_path):
    """Tier 2: omitting ``offset`` / ``limit`` preserves the prior
    full-body / max_bytes-only behaviour — backwards-compatible.
    """
    store, path_ref = _populate_tool_result(tmp_path, "alpha\nbeta\ngamma\n")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref}, ctx))

    assert result["status"] == "ok"
    assert result["content"] == "alpha\nbeta\ngamma\n"
    assert result["truncated"] is False


# ── resource_uri input (= #385 β core impl sub-task 1 dispatcher) ──────


def _populate_with_agent_name(
    tmp_path: Path, agent_name: str, content: str = "hello\nworld\n",
) -> tuple[MediaStore, dict]:
    """Build a MediaStore WITH agent_name, write a tool result, return
    (store, full path-ref block). Block includes resource_uri because
    agent_name was set at construction.
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name=agent_name,
    )
    block = store.save_tool_result(
        content, mime_type="text/plain",
        chain_id="abc123", tool="web_fetch", seq=1,
    )
    return store, block


def test_read_tool_result_accepts_resource_uri_same_host(tmp_path):
    """Tier 2: a path-ref's ``resource_uri`` can be passed in place of
    ``path``; same-host dispatch resolves through MediaStore's
    ``read_tool_result_by_uri`` and returns the body.
    """
    store, block = _populate_with_agent_name(tmp_path, "researcher")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle({"resource_uri": block["resource_uri"]}, ctx),
    )

    assert result["status"] == "ok"
    assert result["content"] == "hello\nworld\n"
    # Identifier echo = resource_uri (= what the LLM supplied), not path.
    assert result["path"] == block["resource_uri"]


def test_read_tool_result_cross_host_resource_uri_returns_stub_error(tmp_path):
    """Tier 2: a resource_uri whose source_agent doesn't match the local
    store's identity returns a structured error (= sub-task 3 will lift
    this; the stub message is the dispatcher contract today).
    """
    # Local store identity = "local-agent"; URI claims source = "remote".
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="local-agent",
    )
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle(
            {"resource_uri": "reyn-tool-result://remote/some-file.txt"},
            ctx,
        ),
    )

    assert result["status"] == "error"
    assert "cross-host" in result["error"]


def test_read_tool_result_invalid_resource_uri_returns_error(tmp_path):
    """Tier 2: a malformed resource_uri surfaces a structured error
    rather than crashing.
    """
    store, _ = _populate_with_agent_name(tmp_path, "me")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle({"resource_uri": "not-a-real-uri"}, ctx),
    )

    assert result["status"] == "error"
    assert "invalid" in result["error"].lower() or "resource_uri" in result["error"]


def test_read_tool_result_rejects_both_path_and_resource_uri(tmp_path):
    """Tier 2: the schema is "exactly one of"; supplying both is a
    contract violation that surfaces as a structured error.
    """
    store, block = _populate_with_agent_name(tmp_path, "me")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle(
            {
                "path": block["path"],
                "resource_uri": block["resource_uri"],
            },
            ctx,
        ),
    )

    assert result["status"] == "error"
    assert "exactly one" in result["error"]


def test_read_tool_result_resource_uri_supports_offset_limit_slice(tmp_path):
    """Tier 2: the same line-slice contract applies when the read goes
    through ``resource_uri`` — offset/limit are orthogonal to the
    addressing mode (path vs URI).
    """
    store, block = _populate_with_agent_name(
        tmp_path, "me", content="L0\nL1\nL2\nL3\nL4\n",
    )
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle(
            {
                "resource_uri": block["resource_uri"],
                "offset": 1,
                "limit": 2,
            },
            ctx,
        ),
    )

    assert result["status"] == "ok"
    assert result["content"] == "L1\nL2\n"


# ── tool_result_read event emission (#385 β core impl sub-task 2) ──────


def _only_tool_result_read(events: _StubEvents) -> list[dict]:
    """Filter the events log down to ``tool_result_read`` payloads.

    Returns the kwargs dict from each emit call; the event name is
    asserted on by checking the list is non-empty (= the handler made
    at least one tool_result_read emit during this dispatch).
    """
    return [kw for name, kw in events.emitted if name == "tool_result_read"]


def test_emits_event_on_success_with_path(tmp_path):
    """Tier 2: a successful path-based read emits ``tool_result_read``
    with status=ok, identifier_kind=path, source_agent=local, and
    sliced/truncated False (= the basic-success payload sub-task 6
    measurement will key off).
    """
    store, path_ref = _populate_tool_result(tmp_path, "hello\n")
    events = _StubEvents()
    ctx = _ctx_with_media_store(store, events=events)

    asyncio.run(_handle({"path": path_ref}, ctx))

    emits = _only_tool_result_read(events)
    assert len(emits) == 1
    payload = emits[0]
    assert payload["status"] == "ok"
    assert payload["identifier_kind"] == "path"
    assert payload["identifier"] == path_ref
    assert payload["source_agent"] == "local"
    assert payload["sliced"] is False
    assert payload["truncated"] is False
    assert payload["total_bytes"] == len("hello\n".encode("utf-8"))
    assert payload["returned_bytes"] == payload["total_bytes"]


def test_emits_event_on_success_with_resource_uri_carries_source_agent(tmp_path):
    """Tier 2: a successful resource_uri read emits source_agent =
    the agent extracted from the URI, not "local". Lets measurement
    distinguish cross-host expand attempts from local ones.
    """
    store, block = _populate_with_agent_name(tmp_path, "researcher")
    events = _StubEvents()
    ctx = _ctx_with_media_store(store, events=events)

    asyncio.run(_handle({"resource_uri": block["resource_uri"]}, ctx))

    payload = _only_tool_result_read(events)[0]
    assert payload["status"] == "ok"
    assert payload["identifier_kind"] == "resource_uri"
    assert payload["source_agent"] == "researcher"


def test_emits_event_with_sliced_true_when_offset_or_limit(tmp_path):
    """Tier 2: when offset / limit are supplied, ``sliced=True`` lets
    the measurement pipeline count partial-read frequency separately
    from full-body reads.
    """
    store, path_ref = _populate_tool_result(tmp_path, "L0\nL1\nL2\n")
    events = _StubEvents()
    ctx = _ctx_with_media_store(store, events=events)

    asyncio.run(_handle({"path": path_ref, "limit": 1}, ctx))

    payload = _only_tool_result_read(events)[0]
    assert payload["sliced"] is True


def test_emits_event_with_truncated_true_when_max_bytes_hit(tmp_path):
    """Tier 2: when max_bytes truncation fires, ``truncated=True`` and
    ``returned_bytes < total_bytes``. Measurement can flag "LLM may
    need a follow-up call" from this signal alone.
    """
    store, path_ref = _populate_tool_result(tmp_path, "a" * 5000)
    events = _StubEvents()
    ctx = _ctx_with_media_store(store, events=events)

    asyncio.run(_handle({"path": path_ref, "max_bytes": 1000}, ctx))

    payload = _only_tool_result_read(events)[0]
    assert payload["truncated"] is True
    assert payload["total_bytes"] == 5000
    assert payload["returned_bytes"] == 1000


def test_emits_event_with_error_kind_missing_args(tmp_path):
    """Tier 2: validation error from neither-arg surfaces in the event
    as ``error_kind=missing_args`` so measurement can distinguish LLM
    contract-violation errors from upstream failures.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    events = _StubEvents()
    ctx = _ctx_with_media_store(store, events=events)

    asyncio.run(_handle({}, ctx))

    payload = _only_tool_result_read(events)[0]
    assert payload["status"] == "error"
    assert payload["error_kind"] == "missing_args"
    assert payload["identifier_kind"] == "missing"


def test_emits_event_with_error_kind_both_supplied(tmp_path):
    """Tier 2: validation error when both path and resource_uri are
    given surfaces as ``error_kind=both_supplied``.
    """
    store, block = _populate_with_agent_name(tmp_path, "me")
    events = _StubEvents()
    ctx = _ctx_with_media_store(store, events=events)

    asyncio.run(_handle({
        "path": block["path"],
        "resource_uri": block["resource_uri"],
    }, ctx))

    payload = _only_tool_result_read(events)[0]
    assert payload["status"] == "error"
    assert payload["error_kind"] == "both_supplied"
    assert payload["identifier_kind"] == "both"


def test_emits_event_with_error_kind_cross_host_stub(tmp_path):
    """Tier 2: a cross-host resource_uri surfaces ``error_kind=
    cross_host_stub`` so measurement can count "LLM tried cross-host
    expand" attempts — exactly the signal sub-task 3 will need to
    prioritise actual cross-host RPC enablement.
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="local",
    )
    events = _StubEvents()
    ctx = _ctx_with_media_store(store, events=events)

    asyncio.run(_handle({
        "resource_uri": "reyn-tool-result://remote/some.txt",
    }, ctx))

    payload = _only_tool_result_read(events)[0]
    assert payload["status"] == "error"
    assert payload["error_kind"] == "cross_host_stub"
    assert payload["identifier_kind"] == "resource_uri"


def test_emits_event_with_error_kind_invalid_uri(tmp_path):
    """Tier 2: a malformed resource_uri surfaces ``error_kind=
    invalid_uri``, distinct from cross-host. Measurement can isolate
    "LLM passed garbage" from "LLM tried real cross-host".
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="me",
    )
    events = _StubEvents()
    ctx = _ctx_with_media_store(store, events=events)

    asyncio.run(_handle({"resource_uri": "not-a-uri"}, ctx))

    payload = _only_tool_result_read(events)[0]
    assert payload["status"] == "error"
    assert payload["error_kind"] == "invalid_uri"


def test_emits_event_with_status_not_found(tmp_path):
    """Tier 2: when the underlying file is missing (= deleted between
    minting and read), the event surfaces ``status=not_found``
    distinct from ``status=error`` — the file existed once, just isn't
    there now.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    store.tool_results_dir.mkdir(parents=True, exist_ok=True)
    fake_rel = str(
        (store.tool_results_dir / "deleted-file.txt").relative_to(tmp_path)
    )
    events = _StubEvents()
    ctx = _ctx_with_media_store(store, events=events)

    asyncio.run(_handle({"path": fake_rel}, ctx))

    payload = _only_tool_result_read(events)[0]
    assert payload["status"] == "not_found"
    assert payload["identifier"] == fake_rel


# ── content_hash verify (#385 β core impl sub-task 4) ─────────────────


def test_content_hash_match_returns_ok(tmp_path):
    """Tier 2: when the LLM passes the path_ref's exact content_hash,
    verification succeeds and the read returns ``status=ok``.
    """
    store, block = _populate_with_agent_name(tmp_path, "me", content="hello\n")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle(
            {"path": block["path"], "content_hash": block["content_hash"]},
            ctx,
        ),
    )

    assert result["status"] == "ok"
    assert result["content"] == "hello\n"


def test_content_hash_mismatch_returns_error(tmp_path):
    """Tier 2: an incorrect content_hash surfaces ``status=error`` with
    ``hash_mismatch`` error_kind plus expected + actual hashes — covers
    file mutation after path_ref minting OR transport corruption.
    """
    store, block = _populate_with_agent_name(tmp_path, "me", content="real\n")
    bogus_hash = "sha256:" + "0" * 64
    events = _StubEvents()
    ctx = _ctx_with_media_store(store, events=events)

    result = asyncio.run(
        _handle(
            {"path": block["path"], "content_hash": bogus_hash},
            ctx,
        ),
    )

    assert result["status"] == "error"
    assert "mismatch" in result["error"]
    # Both hashes surfaced on the response so the LLM can diagnose.
    assert result["expected_hash"] == bogus_hash
    assert result["actual_hash"] == block["content_hash"]
    # Event payload tags the kind + carries both hashes for measurement.
    payload = _only_tool_result_read(events)[0]
    assert payload["status"] == "error"
    assert payload["error_kind"] == "hash_mismatch"
    assert payload["expected_hash"] == bogus_hash
    assert payload["actual_hash"] == block["content_hash"]


def test_content_hash_absent_skips_verify_backward_compat(tmp_path):
    """Tier 2: omitting ``content_hash`` skips verification entirely —
    backward compat for callers that don't carry the hash. Body is
    returned as-is.
    """
    store, path_ref = _populate_tool_result(tmp_path, "body\n")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref}, ctx))

    assert result["status"] == "ok"
    assert result["content"] == "body\n"


def test_content_hash_accepts_bare_hex_no_prefix(tmp_path):
    """Tier 2: ``content_hash`` accepts either ``sha256:<hex>`` (= the
    path_ref's exact form) or bare ``<hex>`` (= LLM might pass just the
    hex after extracting it). Both normalise to the same comparison.
    """
    store, block = _populate_with_agent_name(tmp_path, "me", content="x\n")
    # Strip the "sha256:" prefix to verify the bare-hex acceptance.
    bare_hex = block["content_hash"].removeprefix("sha256:")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle(
            {"path": block["path"], "content_hash": bare_hex},
            ctx,
        ),
    )

    assert result["status"] == "ok"


def test_content_hash_verifies_against_full_body_not_sliced(tmp_path):
    """Tier 2: hash is computed over the FULL body (= before slice /
    truncate). Passing offset/limit alongside a valid content_hash
    still succeeds, because the slice is applied AFTER verification.

    Defence against an attractor where verify-after-slice would force
    callers to compute a different hash for every slice request.
    """
    body = "L0\nL1\nL2\nL3\nL4\n"
    store, block = _populate_with_agent_name(tmp_path, "me", content=body)
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle(
            {
                "path": block["path"],
                "content_hash": block["content_hash"],  # full-body hash
                "offset": 1, "limit": 2,
            },
            ctx,
        ),
    )

    assert result["status"] == "ok"
    # Slice still applied — returned content is the windowed subset.
    assert result["content"] == "L1\nL2\n"


def test_content_hash_works_with_resource_uri_too(tmp_path):
    """Tier 2: verification path is the same whether the read came in
    via ``path`` or ``resource_uri`` — the body is the same body in
    both cases, so the hash check applies uniformly.
    """
    store, block = _populate_with_agent_name(tmp_path, "me", content="hi\n")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle(
            {
                "resource_uri": block["resource_uri"],
                "content_hash": block["content_hash"],
            },
            ctx,
        ),
    )

    assert result["status"] == "ok"
    assert result["content"] == "hi\n"
