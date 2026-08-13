"""Tier 2: #4574 — the artifact-ref MINT/RESOLVE scope mismatch.

Root cause (architect's #4574 issue-thread diagnosis, confirmed by reading
``.reyn/cache/artifact_refs.jsonl`` directly): ``op_runtime/present.py``'s
``handle()`` minted a source-backed artifact's ref under ``ctx.actor`` — a
FIXED per-caller-ROLE literal (``"chat_router"``), never a per-agent
scope — while the TUI's ``_handle_open_artifact_request`` always resolved
it under the real agent NAME (``self._agent_name``, e.g. ``"default"``).
``artifact_ref.py``'s own store is documented "Scope is per-agent" — minting
under one string and resolving under a DIFFERENT one means the ref can
never be found: ``/open`` was ``artifact not found`` unconditionally, for
every source-backed artifact, on every session.

This is the acceptance shape architect asked for explicitly: "test が mint
側と resolve 側で異なる値を渡す経路を通ること" — a prior version of this
class of test could pass vacuously by using the SAME value (e.g. a shared
``tmp_path``/default-agent/``chdir`` fixture) on both sides, which cannot
distinguish "scoped correctly" from "scoped consistently by accident". This
file deliberately uses a caller `actor` DIFFERENT from the live agent's
`agent_name` throughout, and reads back the ACTUAL ref `handle()` minted
(via a real recording ``PresentationRenderer``, never re-derived
independently) — resolving anything OTHER than that captured ref would not
witness this call site's own behavior.

Real ``OpContext``/``Workspace``/``PermissionResolver`` construction
throughout (mirrors ``test_4482_pr2b_artifact_wiring.py``'s own policy) —
no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.present import handle
from reyn.data.workspace.artifact_ref import resolve_ref
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import PresentIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

# Deliberately NOT the same string — the whole point of this file's own
# scoping (see module docstring).
_CALLER_ACTOR = "chat_router"
_LIVE_AGENT_NAME = "default"


class _RecordingRenderer:
    """Real callable standing in for a wired PresentationRenderer — records
    what actually reached the render surface, matching
    ``test_4482_pr2b_artifact_wiring.py``'s own established pattern."""

    surface_name = "inline-cui"

    def __init__(self) -> None:
        self.rendered: list = []

    def render(self, resolved) -> None:
        self.rendered.append(resolved)


def _ctx(tmp_path: Path, renderer: "_RecordingRenderer") -> "tuple[OpContext, EventLog]":
    events = EventLog()
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False)
    ws = Workspace(events=events, permission_resolver=resolver, base_dir=tmp_path)
    ctx = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor=_CALLER_ACTOR,
        agent_name=_LIVE_AGENT_NAME, intervention_bus=None,
        presentation_renderer=renderer,
    )
    return ctx, events


async def _present_and_capture_ref(tmp_path: Path, target: Path) -> str:
    """Drive the REAL ``op_runtime.present.handle()`` end-to-end and return
    the ``ref`` it ACTUALLY minted (read off the rendered node, never
    re-derived by calling ``mint_ref`` a second time — that would witness
    ``mint_ref``'s own idempotency, not this call site's argument choice)."""
    renderer = _RecordingRenderer()
    ctx, _events = _ctx(tmp_path, renderer)
    op = PresentIROp(
        kind="present", data_inline={},
        blueprint={"component": "artifact", "source": str(target)},
    )
    ack = await handle(op, ctx)
    assert ack.get("ok"), ack
    (resolved,) = renderer.rendered
    node = resolved.nodes[0]
    assert node["component"] == "artifact"
    ref = node["body"]["ref"]
    assert isinstance(ref, str) and ref
    return ref


@pytest.mark.asyncio
async def test_a_source_backed_artifacts_ref_resolves_under_the_agent_name_it_was_minted_under(
    tmp_path: Path,
) -> None:
    """Tier 2: the accept-path — present() an artifact from an OpContext
    whose `actor` and `agent_name` DIFFER, capture the ACTUAL ref `handle()`
    minted, then resolve it under the agent name — the SAME
    (project_root, agent_name, ref) shape `app.py`'s
    `_handle_open_artifact_request` calls in production. A regression to
    `ctx.actor` for the mint would make this resolve to `None` (the exact
    "artifact not found" symptom #4574 reported)."""
    target = tmp_path / "report.html"
    target.write_bytes(b"<h1>hi</h1>")

    ref = await _present_and_capture_ref(tmp_path, target)

    resolved = resolve_ref(tmp_path, _LIVE_AGENT_NAME, ref)
    assert resolved == target.resolve()


@pytest.mark.asyncio
async def test_falsify_resolving_the_captured_ref_under_the_callers_actor_finds_nothing(
    tmp_path: Path,
) -> None:
    """Tier 2: LOAD-BEARING falsification — resolving the SAME captured ref
    under `actor` (the pre-#4574 bug's own value, `"chat_router"`) finds
    nothing. Together with the accept-path test above, this proves the ref
    genuinely lives under the agent-name scope and NOT under actor —
    the two tests can only BOTH pass if the mint call site really does use
    `agent_name`; a regression back to `ctx.actor` flips which of the two
    resolves, it does not make both pass or both fail."""
    target = tmp_path / "report.html"
    target.write_bytes(b"<h1>hi</h1>")
    assert _CALLER_ACTOR != _LIVE_AGENT_NAME, (
        "test setup: actor and agent_name must differ, or this file's own "
        "scoping proves nothing"
    )

    ref = await _present_and_capture_ref(tmp_path, target)

    assert resolve_ref(tmp_path, _CALLER_ACTOR, ref) is None
