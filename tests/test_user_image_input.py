"""Tier 2: user image input (issue #366).

The ``/image PATH`` slash command queues an image as a litellm content
block on the session. The next user message drains the queue onto the
ChatMessage.media field, and ``_build_history_for_router`` emits
content as a list of parts for that turn.

We pin:
  - Slash command happy path: file exists, ext supported → queue grows.
  - Slash command rejects unsupported / missing / non-image files.
  - Media gate (= #364) integration: oversize image with on_oversize=deny
    keeps the queue empty + emits error.
  - History builder switches to content-list shape only for messages
    with non-empty media — text-only messages stay string-content
    (= backward compat with existing replay fixtures).
  - ChatMessage.media defaults to empty (= existing callers unaffected).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from reyn.chat.session import ChatMessage
from reyn.config import MultimodalConfig
from reyn.interfaces.slash import REGISTRY
from reyn.security.permissions.permissions import PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention


class _FakeBus:
    def __init__(self, answer: str) -> None:
        self._answer = answer

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        return InterventionAnswer(text="", choice_id=self._answer)


@dataclass
class _OutboxRecord:
    kind: str
    text: str


@dataclass
class _FakeSession:
    """Minimal Session-shaped stand-in for /image testing.

    Holds only the attributes ``image_cmd`` touches: the pending-images
    queue, the multimodal config, the permission resolver, the
    intervention bus, and a captured outbox.
    """
    _multimodal_config: MultimodalConfig | None = None
    _perm: PermissionResolver | None = None
    _intervention_bus: _FakeBus | None = None
    _pending_user_images: list[dict] = field(default_factory=list)
    captured_outbox: list[_OutboxRecord] = field(default_factory=list)

    @property
    def pending_user_images(self) -> list[dict]:
        """Mirror of Session.pending_user_images for the fake stub."""
        return self._pending_user_images

    async def _put_outbox(self, msg: object) -> None:
        self.captured_outbox.append(
            _OutboxRecord(
                kind=getattr(msg, "kind", "system"),
                text=getattr(msg, "text", ""),
            )
        )


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=True,
    )


def _png_bytes(size: int = 200) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)


def _run(coro):
    return asyncio.run(coro)


def _get_image_handler():
    cmd = REGISTRY.get("image")
    assert cmd is not None, "/image slash command should be registered"
    return cmd.handler


# ── happy path ─────────────────────────────────────────────────────────


def test_image_cmd_queues_png(tmp_path, monkeypatch):
    """Tier 2: /image foo.png → block appended to queue; outbox message
    contains the filename + size.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot.png").write_bytes(_png_bytes(500))

    session = _FakeSession(
        _multimodal_config=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"),
        _perm=_resolver(tmp_path),
        _intervention_bus=_FakeBus("yes"),
    )

    handler = _get_image_handler()
    _run(handler(session, "shot.png"))

    assert session.pending_user_images, "expected image queued"
    block = session.pending_user_images[0]
    # Issue #383 PR-C: /image now stores a path-ref to the user's file
    # instead of inlining base64. Storage stays out of history.jsonl.
    assert block["type"] == "image"
    assert block["mime_type"] == "image/png"
    assert "shot.png" in block["path"]
    assert block["content_hash"].startswith("sha256:")
    # outbox confirmation
    assert any("shot.png" in m.text for m in session.captured_outbox)


def test_image_cmd_supports_jpeg_and_alias(tmp_path, monkeypatch):
    """Tier 2: /img alias works; jpeg extension produces image/jpeg mime."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pic.jpg").write_bytes(b"jpeg-payload")

    cmd = REGISTRY.get("img")  # alias
    assert cmd is not None
    session = _FakeSession()
    _run(cmd.handler(session, "pic.jpg"))

    assert session.pending_user_images, "expected image queued"
    block = session.pending_user_images[0]
    assert block["mime_type"] == "image/jpeg"
    assert "pic.jpg" in block["path"]


def test_multiple_image_calls_stack(tmp_path, monkeypatch):
    """Tier 2: two /image calls before the next user message → queue
    holds both, in order.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.png").write_bytes(_png_bytes())
    (tmp_path / "b.png").write_bytes(_png_bytes())

    session = _FakeSession()
    handler = _get_image_handler()
    _run(handler(session, "a.png"))
    _run(handler(session, "b.png"))

    paths = [b["path"] for b in session.pending_user_images]
    assert any("a.png" in p for p in paths)
    assert any("b.png" in p for p in paths)


# ── error paths ────────────────────────────────────────────────────────


def test_image_cmd_empty_path_errors(tmp_path):
    """Tier 2: /image with empty path → queue stays empty; outbox shows usage error."""
    session = _FakeSession()
    handler = _get_image_handler()
    _run(handler(session, ""))

    assert session.pending_user_images == []
    assert any(m.kind == "error" and "usage" in m.text for m in session.captured_outbox)


def test_image_cmd_missing_file_errors(tmp_path, monkeypatch):
    """Tier 2: /image with non-existent file → queue stays empty; outbox shows not-found error."""
    monkeypatch.chdir(tmp_path)
    session = _FakeSession()
    handler = _get_image_handler()
    _run(handler(session, "no-such.png"))

    assert session.pending_user_images == []
    assert any(m.kind == "error" and "not found" in m.text for m in session.captured_outbox)


def test_image_cmd_unsupported_extension_errors(tmp_path, monkeypatch):
    """Tier 2: /image with .txt extension → queue stays empty; outbox shows unsupported error."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_bytes(b"text")
    session = _FakeSession()
    handler = _get_image_handler()
    _run(handler(session, "notes.txt"))

    assert session.pending_user_images == []
    assert any(m.kind == "error" and "unsupported" in m.text for m in session.captured_outbox)


# ── media gate integration (= #364 reuse) ──────────────────────────────


def test_image_cmd_oversize_with_deny_keeps_queue_empty(tmp_path, monkeypatch):
    """Tier 2: oversize image + on_oversize=deny → gate denies, queue
    stays empty, error in outbox.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "huge.png").write_bytes(b"x" * 10_000_000)
    session = _FakeSession(
        _multimodal_config=MultimodalConfig(max_bytes=5_000_000, on_oversize="deny"),
        _perm=_resolver(tmp_path),
        _intervention_bus=_FakeBus("never_called"),
    )

    handler = _get_image_handler()
    _run(handler(session, "huge.png"))

    assert session.pending_user_images == []
    assert any(m.kind == "error" for m in session.captured_outbox)


def test_image_cmd_oversize_with_ask_no_keeps_queue_empty(tmp_path, monkeypatch):
    """Tier 2: oversize image + on_oversize=ask + user-no → gate denies."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "huge.png").write_bytes(b"x" * 10_000_000)
    session = _FakeSession(
        _multimodal_config=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"),
        _perm=_resolver(tmp_path),
        _intervention_bus=_FakeBus("no"),
    )

    handler = _get_image_handler()
    _run(handler(session, "huge.png"))

    assert session.pending_user_images == []


def test_image_cmd_no_multimodal_config_skips_gate(tmp_path, monkeypatch):
    """Tier 2: when session lacks a multimodal_config (= direct test
    construction), the gate is skipped — image still queues.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.png").write_bytes(b"x" * 10_000_000)  # would normally exceed
    session = _FakeSession()  # no multimodal_config, no perm

    handler = _get_image_handler()
    _run(handler(session, "any.png"))

    assert session.pending_user_images, "expected image queued (gate skipped)"


# ── ChatMessage content shape (issue #383 update) ──────────────────────


def test_chat_message_default_content_str_empty():
    """Tier 2: ChatMessage with str content has no media via the derived
    ``.text`` property; constructing without media-typed parts leaves
    the content as a plain string.
    """
    msg = ChatMessage(role="user", content="hi", ts="2026-05-21T00:00:00+00:00")
    assert msg.content == "hi"
    assert msg.text == "hi"  # derived text view


def test_chat_message_content_list_persists_image_block():
    """Tier 2: image block stored in ``content`` (list-of-parts shape)
    survives construction; ``.text`` returns the text-part only.
    """
    block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    msg = ChatMessage(
        role="user",
        content=[{"type": "text", "text": "see this"}, block],
        ts="2026-05-21T00:00:00+00:00",
    )
    assert msg.content == [{"type": "text", "text": "see this"}, block]
    assert msg.text == "see this"


# ── history-builder content-list shape ─────────────────────────────────


def _make_history_builder():
    """Pluck ``_build_history_for_router`` in a minimal harness — we
    only need its content-shape decision logic.

    #1128 step 3: slicing is now token-budget based (effective_trigger from
    engine budgets, falling back to get_max_input_tokens).  Short test turns
    (1-2 messages of a few tokens) are well below any realistic trigger, so
    all turns are returned raw — no elide, no duplication.
    """
    from reyn.chat.services.router_history_buffer import RouterHistoryBuffer
    from reyn.chat.session import Session
    from reyn.config import CompactionConfig

    cs = Session.__new__(Session)  # bypass __init__
    cs.history = []  # set by tests
    cs._compaction = CompactionConfig(use_chars4_estimate=True)
    cs._latest_summary = lambda: None  # type: ignore[method-assign]
    cs._history_buffer = RouterHistoryBuffer(
        history_fn=lambda: cs.history,
        compaction=cs._compaction,
        compaction_controller=None,
        model="",
        events=None,
        media_store=None,
        router_host=None,
        action_retrieval=None,
        non_interactive=False,
    )
    return cs


def test_history_text_only_emits_string_content():
    """Tier 2: text-only ChatMessage → string content (= backward compat
    with all existing replay fixtures + prompt cache).
    """
    cs = _make_history_builder()
    cs.history = [
        ChatMessage(role="user", content="hi", ts="t1"),
        ChatMessage(role="assistant", content="hello", ts="t2"),
    ]
    msgs = cs._build_history_for_router()

    assert msgs[0] == {"role": "user", "content": "hi"}
    assert msgs[1] == {"role": "assistant", "content": "hello"}


def test_history_message_with_media_emits_content_list():
    """Tier 2: ChatMessage with multimodal content → wire shape is a
    list of litellm parts (pass-through, no synthesis).
    """
    cs = _make_history_builder()
    block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    cs.history = [
        ChatMessage(
            role="user",
            content=[{"type": "text", "text": "look at this"}, block],
            ts="t1",
        ),
    ]
    msgs = cs._build_history_for_router()

    assert msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    assert isinstance(content, list)
    assert content == [{"type": "text", "text": "look at this"}, block]


def test_history_message_with_media_and_no_text_emits_only_image() -> None:
    """Tier 2: a message with media but no text (= edge case, /image
    sent without typing) still produces a valid content list with just
    the image part — no empty text block leaked.
    """
    cs = _make_history_builder()
    block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    cs.history = [ChatMessage(role="user", content=[block], ts="t1")]
    msgs = cs._build_history_for_router()

    assert msgs[0]["content"] == [block]
