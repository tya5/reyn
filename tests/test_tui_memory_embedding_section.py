"""Tier 2: FP-0043 Component C.4 — TUI Memory tab embedding section contract.

Pins the surface invariants from end to end:

  Producer side (ChatLifecycleForwarder):
    1. ``on_embedding_status`` / ``on_embedding_skill_done`` /
       ``on_embedding_error`` each emit an ``OutboxMessage`` carrying the
       full meta dict so the TUI Memory tab subscriber sees ``model`` /
       ``device`` / ``dimension`` / ``retry_hint`` as appropriate.
    2. The outbox kind matches the event-type prefix so the TUI handler
       dispatch table routes correctly.

  Renderer side (render_memory + _render_embedding_section):
    3. No ``embedding_state`` → no EMBEDDINGS section in the output.
    4. ``embedding_status`` state → "loading…" row with model + device.
    5. ``embedding_skill_done`` state → "loaded" row with model + dim.
    6. ``embedding_error`` state → "error" row plus retry-hint subline
       (truncated to fit narrow panel).
    7. The ``sentence-transformers/`` prefix is dropped from model
       strings so wide HF ids fit a default-width panel.

No mocks. Tests use a real ``ChatLifecycleForwarder`` against a real
``asyncio.Queue``, and call ``render_memory`` directly with a tmp
project root.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.interfaces.tui.widgets.right_panel.memory_tab import render_memory
from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder
from reyn.schemas.models import Event


def _make_forwarder() -> tuple[ChatLifecycleForwarder, asyncio.Queue]:
    """Build a forwarder + the asyncio.Queue it pushes outbox messages into."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        outbox: asyncio.Queue = asyncio.Queue()
    finally:
        loop.close()
    return ChatLifecycleForwarder(outbox), outbox


def _drain(queue: asyncio.Queue) -> list[Any]:
    """Pop all items currently in the queue without awaiting."""
    items: list[Any] = []
    while True:
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return items


# ── 1. Lifecycle forwarder emits OutboxMessage with full meta ────────────────


def test_on_embedding_status_emits_outbox_message_with_meta() -> None:
    """Tier 2: ``embedding_status`` event → outbox kind preserves meta."""
    fwd, outbox = _make_forwarder()
    fwd(Event(type="embedding_status", data={
        "text": "loading model X",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "target_dir": "/tmp/cache/sentence-transformers",
        "device": "cpu",
    }))
    msgs = _drain(outbox)
    assert [m.kind for m in msgs] == ["embedding_status"]
    msg = msgs[0]
    assert msg.text == "loading model X"
    assert msg.meta["model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert msg.meta["device"] == "cpu"
    assert msg.meta["target_dir"] == "/tmp/cache/sentence-transformers"


def test_on_embedding_skill_done_emits_outbox_message_with_dimension() -> None:
    """Tier 2: ``embedding_skill_done`` carries ``dimension`` for UX."""
    fwd, outbox = _make_forwarder()
    fwd(Event(type="embedding_skill_done", data={
        "text": "loaded",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "dimension": 384,
    }))
    msgs = _drain(outbox)
    assert [m.kind for m in msgs] == ["embedding_skill_done"]
    assert msgs[0].meta["dimension"] == 384


def test_on_embedding_error_emits_outbox_message_with_retry_hint() -> None:
    """Tier 2: ``embedding_error`` carries ``retry_hint`` for self-correction."""
    fwd, outbox = _make_forwarder()
    fwd(Event(type="embedding_error", data={
        "text": "failed to load",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "retry_hint": "Run `reyn embeddings clear` to wipe partial state.",
    }))
    msgs = _drain(outbox)
    assert [m.kind for m in msgs] == ["embedding_error"]
    assert "reyn embeddings clear" in msgs[0].meta["retry_hint"]


# ── 2. Renderer suppresses section when no state observed ────────────────────


def test_render_memory_omits_embedding_section_when_state_none(
    tmp_path: Path,
) -> None:
    """Tier 2: ``embedding_state=None`` (= default) → no EMBEDDINGS section."""
    rendered, _entries, _ys = render_memory(
        tmp_path, embedding_state=None,
    )
    assert "EMBEDDINGS" not in rendered


def test_render_memory_omits_embedding_section_when_state_missing_kwarg(
    tmp_path: Path,
) -> None:
    """Tier 2: caller may omit the new kwarg entirely (= forward compat)."""
    rendered, _entries, _ys = render_memory(tmp_path)
    assert "EMBEDDINGS" not in rendered


# ── 3. Renderer surfaces each lifecycle kind correctly ───────────────────────


def test_render_memory_status_shows_loading_with_model_and_device(
    tmp_path: Path,
) -> None:
    """Tier 2: ``embedding_status`` → loading row carrying model + device."""
    state = {
        "kind": "embedding_status",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "device": "cpu",
        "target_dir": "/tmp/cache",
    }
    rendered, _, _ = render_memory(tmp_path, embedding_state=state)
    assert "EMBEDDINGS" in rendered
    assert "loading" in rendered
    # ST prefix dropped so the model fits a narrow panel.
    assert "all-MiniLM-L6-v2" in rendered
    assert "sentence-transformers/" not in rendered
    assert "cpu" in rendered


def test_render_memory_skill_done_shows_loaded_with_dimension(
    tmp_path: Path,
) -> None:
    """Tier 2: ``embedding_skill_done`` → loaded row + Nd dimension suffix."""
    state = {
        "kind": "embedding_skill_done",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "dimension": 384,
    }
    rendered, _, _ = render_memory(tmp_path, embedding_state=state)
    assert "loaded" in rendered
    assert "384d" in rendered
    assert "all-MiniLM-L6-v2" in rendered


def test_render_memory_error_shows_retry_hint_subline(tmp_path: Path) -> None:
    """Tier 2: ``embedding_error`` → error row + truncated retry-hint subline."""
    state = {
        "kind": "embedding_error",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "retry_hint": (
            "Run `reyn embeddings clear` to wipe the cache + check "
            "network connectivity + retry."
        ),
    }
    rendered, _, _ = render_memory(tmp_path, embedding_state=state)
    assert "error" in rendered
    # Hint is included (possibly truncated at the panel-width boundary).
    assert "reyn embeddings clear" in rendered


def test_render_memory_truncates_very_long_retry_hint(tmp_path: Path) -> None:
    """Tier 2: a 200-char retry hint is truncated with an ellipsis suffix.

    Default panel width is ~33% of the terminal; long hints would wrap
    awkwardly and obscure other entries. The Events tab carries the
    full text.
    """
    long_hint = (
        "Run `reyn embeddings clear` to wipe the cache, then "
        + ("verify network and ensure proxy is reachable. " * 5)
    )
    state = {
        "kind": "embedding_error",
        "model": "x",
        "retry_hint": long_hint,
    }
    rendered, _, _ = render_memory(tmp_path, embedding_state=state)
    # Truncation marker present; the full long_hint is NOT verbatim.
    assert "…" in rendered or long_hint not in rendered


def test_render_memory_handles_non_st_prefixed_model(tmp_path: Path) -> None:
    """Tier 2: a non-``sentence-transformers/`` model passes through verbatim.

    Future backends (= ONNX / GGUF) reaching this surface should display
    their model id without the prefix-strip path mangling them.
    """
    state = {
        "kind": "embedding_skill_done",
        "model": "openai/text-embedding-3-small",
        "dimension": 1536,
    }
    rendered, _, _ = render_memory(tmp_path, embedding_state=state)
    assert "openai/text-embedding-3-small" in rendered
    assert "1536d" in rendered


# ── 4. Existing sections preserved when embedding section renders ────────────


def test_render_memory_embedding_section_does_not_break_hot_list(
    tmp_path: Path,
) -> None:
    """Tier 2: HOT NOW + EMBEDDINGS coexist; no section is silently dropped."""
    state = {"kind": "embedding_skill_done", "model": "x", "dimension": 8}
    hot_list = [{"qualified_name": "file__read", "freq": 5, "last_ts": 0.0}]
    rendered, _, _ = render_memory(
        tmp_path,
        hot_list=hot_list,
        embedding_state=state,
    )
    assert "HOT NOW" in rendered
    assert "EMBEDDINGS" in rendered
    # The embedding section comes AFTER the HOT NOW section
    # (= structural ordering pinned by render_memory: hot → scopes →
    # embeddings).
    assert rendered.index("HOT NOW") < rendered.index("EMBEDDINGS")


def test_render_memory_unknown_kind_falls_back_to_text(tmp_path: Path) -> None:
    """Tier 2: future emitter kind shows the raw text rather than blank.

    Pinned so a new ``embedding_*`` kind landing without a matching
    render branch still produces SOME row instead of a silent section
    header.
    """
    state = {
        "kind": "embedding_unknown_future_kind",
        "text": "novel state",
        "model": "x",
    }
    rendered, _, _ = render_memory(tmp_path, embedding_state=state)
    assert "EMBEDDINGS" in rendered
    assert "novel state" in rendered
