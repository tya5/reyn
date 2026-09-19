"""#6230 stage 1: ``text`` is human-readable; the control payload moves to
``meta``.

Two slash commands put a CONTROL value in ``OutboxMessage.text`` where the
human-readable representation belongs (issue thread, ruled on by architect +
lead-coder): ``/open`` (``open_artifact.py``) forwarded the raw artifact ref
verbatim, ``/copy`` (``copy.py``) forwarded the bare argument verbatim. Both
now build ``text`` as a legible fallback ("what a person should see if their
surface cannot act on the control payload") and move the value a
reyn-aware consumer needs into ``meta`` — the private channel only such a
consumer reads (``text`` rides the standard AG-UI channel and reaches a
client with zero reyn-specific rendering).

``__rewind_list__`` (``rewind.py``) is deliberately UNCHANGED here — it
already puts a genuinely legible list in ``text`` (contrast cited in the
issue thread) — and this stage does not touch the web client's
``kind.startsWith("__")`` guard (stage 2) or ``OutboxMessage.__post_init__``
(stage 3).

Real instances throughout — no ``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys

import pytest

from reyn.data.workspace.artifact_ref import mint_ref
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.slash.copy import copy_cmd
from reyn.interfaces.slash.open_artifact import open_cmd
from reyn.runtime.outbox import OutboxMessage
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML
from tests._support.slash import slash_ctx
from tests._support.textual_chat_test_helpers import QueueTransport

# ── /open: sentinel construction ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_cmd_puts_the_ref_in_meta() -> None:
    """Tier 2: /open <ref> puts the ref in ``meta["ref"]`` — the control
    channel :meth:`TextualChatApp._pump_frames` reads."""
    ctx = slash_ctx()
    await open_cmd(ctx, "agent:default/artifacts/q1/report.pptx")
    [msg] = [m for m in ctx.transport.displayed if m.kind == "__open_artifact__"]
    assert msg.meta["ref"] == "agent:default/artifacts/q1/report.pptx"


@pytest.mark.asyncio
async def test_open_cmd_text_is_human_readable_not_the_raw_ref() -> None:
    """Tier 2: /open's ``text`` is a legible sentence, never the bare ref —
    the defect this stage closes."""
    ctx = slash_ctx()
    ref = "agent:default/artifacts/q1/report.pptx"
    await open_cmd(ctx, ref)
    [msg] = [m for m in ctx.transport.displayed if m.kind == "__open_artifact__"]
    assert msg.text != ref, "text must not be the bare control value"
    assert ref in msg.text, "text should still name the ref, legibly"


@pytest.mark.asyncio
async def test_open_cmd_with_no_ref_still_yields_legible_text() -> None:
    """Tier 2: an empty /open still gets a real (non-empty, non-blank)
    ``text`` — no client is left with nothing to show."""
    ctx = slash_ctx()
    await open_cmd(ctx, "")
    [msg] = [m for m in ctx.transport.displayed if m.kind == "__open_artifact__"]
    assert msg.text.strip()
    assert msg.meta["ref"] == ""


# ── /copy: sentinel construction ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_copy_cmd_with_no_arg_still_yields_legible_text() -> None:
    """Tier 2: a bare /copy (no arg) still gets a real, non-empty ``text``."""
    ctx = slash_ctx()
    await copy_cmd(ctx, "")
    [msg] = [m for m in ctx.transport.displayed if m.kind == "__copy_last_reply__"]
    assert msg.text.strip()
    assert msg.meta["arg"] == ""


# ── /open: the app reads the ref from meta, not text ─────────────────────


@pytest.mark.asyncio
async def test_app_reads_open_artifact_ref_from_meta_not_text(tmp_path, monkeypatch):
    """Tier 2: pushed as a real ``__open_artifact__`` DisplayFrame (the same
    shape :meth:`TextualChatApp._pump_frames` consumes off the transport
    stream) with ``text`` holding an unrelated human sentence and
    ``meta["ref"]`` holding the real ref, the app opens THAT ref — proving
    the pump loop reads ``meta``, never ``text``, for the control value.

    Real ``resolve_ref`` against a real minted ref, real subprocess launch
    to a fake opener on PATH — mirrors
    ``test_artifact_list_and_open_4482.py``'s own end-to-end pattern, but
    driven through the PUMP LOOP (a real frame off the transport stream)
    rather than by calling the handler method directly, so this test is
    the one that would catch a regression back to reading ``msg.text``.
    """
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    target = tmp_path / "report.pptx"
    target.write_text("fake pptx bytes")
    ref = mint_ref(tmp_path, "default", target)

    opener_name = "open" if sys.platform == "darwin" else "xdg-open"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "opened.txt"
    sink_tmp = tmp_path / "opened.txt.tmp"
    script = bindir / opener_name
    script.write_text(f'#!/bin/sh\necho "$1" > {sink_tmp}\nmv {sink_tmp} {sink}\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(tmp_path)

    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test() as pilot:
        await pilot.pause()
        await transport.push(OutboxMessage(
            kind="__open_artifact__",
            text="this sentence is not a control value",
            meta={"ref": ref},
        ))
        await pilot.pause()

        # Unbounded — CI's own --timeout=120 is the kill switch (testing.md
        # ceiling rule). ``asyncio.sleep(0)`` is a cooperative yield, not a
        # wait duration this assertion depends on.
        while not sink.exists():
            await asyncio.sleep(0)
        assert sink.read_text().strip() == str(target), (
            "the app must resolve the opened artifact from meta['ref'], "
            "never from text"
        )
