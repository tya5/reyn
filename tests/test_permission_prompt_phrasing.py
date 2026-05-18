"""Tier 2: per-gate natural-language phrasing for permission prompts (#224).

Pre-fix, every permission prompt header was the generic
``"Permission request — {key}"`` form, exposing internal config keys
(``web.fetch``, ``mcp.<server>``, ``shell``, …) as the user-facing
prompt header. Light-users had to mentally translate a config key into
"what is the agent asking me?".

Per the issue's direction (b), each ``require_*`` method now passes
a ``user_prompt`` argument with a natural-language question, while
the underlying ``_approve`` / ``_prompt`` machinery preserves the
existing ``"Permission request — {key}"`` fallback for any caller
that hasn't migrated.

This file pins:
  1. ``_prompt`` uses ``user_prompt`` when present, falls back when None.
  2. Each migrated ``require_*`` passes a sensible natural prompt.
  3. The verify-script JSON shape (= production path) carries the
     natural prompt in ``meta.prompt`` — TUI widget consumes it.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.chat.outbox import OutboxMessage
from reyn.chat.services.intervention_handler import InterventionHandler
from reyn.intervention_choices import generic_yn_choices
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionBus,
    UserIntervention,
)

# ── helpers ────────────────────────────────────────────────────────────────


class _RecordingBus:
    """Captures the UserIntervention passed to request() and returns a
    pre-set answer. Real production path; no MagicMock."""

    def __init__(self, answer_id: str = "no") -> None:
        self.captured: list[UserIntervention] = []
        self._answer_id = answer_id

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.captured.append(iv)
        return InterventionAnswer(text=self._answer_id, choice_id=self._answer_id)


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={},  # nothing pre-approved → goes to interactive
        project_root=tmp_path,
        interactive=True,
    )


# ── 1. _approve / _prompt accept user_prompt override ───────────────────


@pytest.mark.asyncio
async def test_prompt_uses_user_prompt_when_provided(tmp_path) -> None:
    """Tier 2: when user_prompt is passed, it becomes the prompt header."""
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="no")
    await r._approve("test.key", "test description", bus, user_prompt="Allow this thing?")
    assert len(bus.captured) == 1
    iv = bus.captured[0]
    assert iv.prompt == "Allow this thing?"
    # detail still gets the description.
    assert iv.detail == "test description"


@pytest.mark.asyncio
async def test_prompt_falls_back_to_key_form_when_user_prompt_none(tmp_path) -> None:
    """Tier 2: legacy "Permission request — {key}" form preserved on omission.

    Backward-compat: any caller that hasn't migrated to user_prompt= keeps
    the original behavior. Used by tests and non-yet-migrated require_*
    helpers (none today, but the contract is reserved).
    """
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="no")
    await r._approve("test.fallback", "fallback description", bus)
    assert bus.captured[0].prompt == "Permission request — test.fallback"


# ── 2. require_web_fetch uses natural prompt ─────────────────────────────


@pytest.mark.asyncio
async def test_require_web_fetch_prompt_is_natural(tmp_path) -> None:
    """Tier 2: require_web_fetch passes a natural-language user_prompt."""
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="no")
    try:
        await r.require_web_fetch("https://example.com", bus)
    except PermissionError:
        pass  # expected — we answered "no"
    assert len(bus.captured) == 1
    iv = bus.captured[0]
    assert iv.prompt == "Allow fetching this URL?"
    # detail carries the URL so user can verify what's being fetched.
    assert "https://example.com" in iv.detail
    # The config key (web.fetch) is NOT in the prompt header.
    assert "web.fetch" not in iv.prompt


# ── 3. require_shell uses natural prompt ─────────────────────────────────


@pytest.mark.asyncio
async def test_require_shell_prompt_is_natural(tmp_path) -> None:
    """Tier 2: require_shell passes a natural-language user_prompt."""
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="no")
    try:
        await r.require_shell(PermissionDecl(shell=True), "ls -la", bus)
    except PermissionError:
        pass
    assert len(bus.captured) == 1
    iv = bus.captured[0]
    assert iv.prompt == "Allow running this shell command?"
    assert "ls -la" in iv.detail
    assert "shell" not in iv.prompt.lower() or "command" in iv.prompt.lower()


# ── 4. End-to-end announce: meta.prompt carries natural phrasing ────────


async def _capture_announce(iv: UserIntervention) -> OutboxMessage:
    """Run the production announce() path and capture the produced msg."""
    captured: list[OutboxMessage] = []

    async def _put(msg: OutboxMessage) -> None:
        captured.append(msg)

    handler = InterventionHandler(
        intervention_registry=None,
        journal=None,
        event_log=None,
        put_outbox=_put,
        append_history=lambda *_a, **_k: None,
    )
    await handler.announce(iv)
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_announce_meta_carries_natural_prompt() -> None:
    """Tier 2: the natural prompt flows through announce() into meta.prompt.

    Pins the end-to-end: TUI widget reads meta.prompt → renders as
    amber-bold header. Light-users see "Allow fetching this URL?"
    instead of "Permission request — web.fetch".
    """
    iv = UserIntervention(
        kind="permission.generic",
        prompt="Allow fetching this URL?",
        detail="web fetch: https://example.com",
        choices=generic_yn_choices(),
        run_id="r1",
        skill_name="chat_router",
    )
    msg = await _capture_announce(iv)
    assert msg.meta["prompt"] == "Allow fetching this URL?"
    assert msg.meta["detail"] == "web fetch: https://example.com"
    # msg.text (CLI Panel renderer backward-compat) still has it all
    assert "Allow fetching this URL?" in msg.text
    assert "https://example.com" in msg.text


# ── 5. require_mcp + require_mcp_install + require_index_drop +
#     require_mcp_drop_server + require_tool + require_python — natural ────


@pytest.mark.asyncio
async def test_require_mcp_prompt_is_natural(tmp_path) -> None:
    """Tier 2: require_mcp passes natural prompt mentioning the server name."""
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="no")
    decl = PermissionDecl(allowed_mcp=["filesystem"], mcp=["filesystem"])
    try:
        await r.require_mcp(decl, "filesystem", bus)
    except PermissionError:
        pass
    iv = bus.captured[0]
    assert "filesystem" in iv.prompt
    assert iv.prompt.lower().startswith("allow")  # natural-language style


@pytest.mark.asyncio
async def test_require_tool_prompt_is_natural(tmp_path) -> None:
    """Tier 2: require_tool prompts use natural phrasing including the tool name."""
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="no")
    decl = PermissionDecl(tool=["web_search"])
    try:
        await r.require_tool(decl, "web_search", bus)
    except PermissionError:
        pass
    iv = bus.captured[0]
    assert "web_search" in iv.prompt
    assert iv.prompt.lower().startswith("allow")
