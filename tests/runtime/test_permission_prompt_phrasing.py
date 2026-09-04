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
  1. Each migrated ``require_*`` passes a sensible natural prompt.
  2. The verify-script JSON shape (= production path) carries the
     natural prompt in ``meta.prompt`` — TUI widget consumes it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.intervention_choices import generic_yn_choices
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.services.intervention_handler import InterventionHandler
from reyn.runtime.services.intervention_registry import InterventionRegistry
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import (
    InterventionAnswer,
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


# ── 1. require_web_fetch uses natural prompt ────────────────────────────


@pytest.mark.asyncio
async def test_require_web_fetch_prompt_is_natural(tmp_path) -> None:
    """Tier 2: require_web_fetch passes a natural-language user_prompt."""
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="no")
    try:
        await r.require_web_fetch("https://example.com", bus)
    except PermissionError:
        pass  # expected — we answered "no"
    (iv,) = bus.captured  # exactly one intervention requested
    assert iv.prompt == "Allow fetching this URL?"
    # detail carries the URL so user can verify what's being fetched.
    assert "https://example.com" in iv.detail
    # The config key (web.fetch) is NOT in the prompt header.
    assert "web.fetch" not in iv.prompt


# ── 2. End-to-end announce: meta.prompt carries natural phrasing ────────


async def _capture_announce(iv: UserIntervention, tmp_path: Path) -> OutboxMessage:
    """Run the production announce() path and capture the produced msg."""
    captured: list[OutboxMessage] = []

    async def _put(msg: OutboxMessage) -> None:
        captured.append(msg)

    async def _on_announce(_iv: UserIntervention) -> None:
        pass

    handler = InterventionHandler(
        # #5739 (lead-coder's own follow-up finding on this exact file,
        # same night): NOT a type:ignore, even though announce() — the
        # only method this helper calls — never touches either TODAY.
        # That was the EXACT reasoning that made event_log=None silently
        # green here for hours until #5734 added one line to announce()
        # that DID touch it, costing a CI round-trip. Both are cheap to
        # construct for real (CLAUDE.md: "never fake a collaborator when
        # a real instance is cheaply constructible") — a real
        # InterventionRegistry just needs a no-op async on_announce
        # callback; a real SnapshotJournal with state_log=None disables
        # persistence (its own documented, intentional no-op mode) and
        # needs only an agent_name + a throwaway snapshot_path.
        intervention_registry=InterventionRegistry(on_announce=_on_announce),
        journal=SnapshotJournal(
            agent_name="permission-prompt-phrasing-test",
            snapshot_path=tmp_path / "snapshot.json",
            state_log=None,
        ),
        event_log=EventLog(),
        put_outbox=_put,
        append_history=lambda *_a, **_k: None,
    )
    await handler.announce(iv)
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_announce_meta_carries_natural_prompt(tmp_path: Path) -> None:
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
        actor="chat_router",
    )
    msg = await _capture_announce(iv, tmp_path)
    assert msg.meta["prompt"] == "Allow fetching this URL?"
    assert msg.meta["detail"] == "web fetch: https://example.com"
    # msg.text (CLI Panel renderer backward-compat) still has it all
    assert "Allow fetching this URL?" in msg.text
    assert "https://example.com" in msg.text


# ── 4. require_mcp + require_tool — natural ────────────────────────────────
#
# #571 collapse arc Phase 5: the per-op interactive prompts for
# require_mcp_install / require_index_drop / require_mcp_drop_server /
# require_cron_register were removed alongside the bool-axis resolver
# methods themselves. Authorisation flows through ``require_file_write``
# (no interactive prompt at runtime) — operator consent is collected
# via the runtime prompt for the canonical file.write paths.


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
