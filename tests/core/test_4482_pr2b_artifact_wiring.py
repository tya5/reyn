"""Tier 1/2: #4482 PR-2b — the "artifact" present component's wiring:
catalog.py's structural gate, binding.py's pure resolution branch,
artifact_payload.apply_artifact_resolution's post-processing pass, and
op_runtime/present.py's handle() end-to-end.

Real Workspace + PermissionResolver + EventLog throughout (matches
test_present_op_fp0054_pra.py's own established policy) — no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.present import handle
from reyn.core.present import PresentBlueprintError, resolve_bindings, validate_blueprint
from reyn.core.present.artifact_payload import apply_artifact_resolution
from reyn.data.workspace.artifact_ref import resolve_ref
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import PresentIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False)


def _ctx(tmp_path: Path) -> "tuple[OpContext, EventLog]":
    events = EventLog()
    resolver = _resolver(tmp_path)
    ws = Workspace(events=events, permission_resolver=resolver, base_dir=tmp_path)
    ctx = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="alice", intervention_bus=None,
    )
    return ctx, events


def _run(coro):
    return asyncio.run(coro)


# ── catalog.py: structural gate ──────────────────────────────────────────


def test_source_alone_is_valid():
    """Tier 1: accept-side — source + description, no media_type."""
    nodes = validate_blueprint(
        {"component": "artifact", "source": "report.pptx", "description": "the report"},
    )
    assert nodes[0]["source"] == "report.pptx"


def test_content_with_media_type_is_valid():
    """Tier 1: accept-side — content + media_type together."""
    nodes = validate_blueprint(
        {"component": "artifact", "content": "a,b\n1,2\n", "media_type": "text/csv"},
    )
    assert nodes[0]["content"] == "a,b\n1,2\n"
    assert nodes[0]["media_type"] == "text/csv"


def test_both_source_and_content_is_rejected():
    """Tier 1: exactly one of source/content — both is a hard rejection."""
    with pytest.raises(PresentBlueprintError):
        validate_blueprint({"component": "artifact", "source": "x", "content": "y"})


def test_neither_source_nor_content_is_rejected():
    """Tier 1: exactly one of source/content — neither is a hard rejection."""
    with pytest.raises(PresentBlueprintError):
        validate_blueprint({"component": "artifact", "description": "no target"})


def test_media_type_alongside_source_is_rejected():
    """Tier 1: media_type is forbidden alongside source — the OS derives it
    from the real file in that case."""
    with pytest.raises(PresentBlueprintError):
        validate_blueprint(
            {"component": "artifact", "source": "x", "media_type": "text/csv"},
        )


def test_content_without_media_type_is_rejected():
    """Tier 1: media_type is REQUIRED alongside content — with no real
    file, there is nothing for the OS to derive it from."""
    with pytest.raises(PresentBlueprintError):
        validate_blueprint({"component": "artifact", "content": "just text"})


def test_name_is_not_an_accepted_slot():
    """Tier 1: name is deliberately not a slot — the OS fills it, the
    agent cannot write it."""
    with pytest.raises(PresentBlueprintError):
        validate_blueprint(
            {"component": "artifact", "source": "x", "name": "sneaky.txt"},
        )


# ── binding.py: pure resolution ──────────────────────────────────────────


def test_resolve_bindings_resolves_a_literal_source():
    """Tier 1: a literal source string passes through resolve_bindings
    unchanged (same as image's own src literal path)."""
    nodes = validate_blueprint({"component": "artifact", "source": "report.pptx"})
    out = resolve_bindings(nodes, {})
    assert out.nodes[0]["source"] == "report.pptx"


def test_resolve_bindings_resolves_a_bound_source():
    """Tier 1: a $bind source resolves against the doc, same as any other
    text-family slot."""
    nodes = validate_blueprint({"component": "artifact", "source": {"$bind": "/path"}})
    out = resolve_bindings(nodes, {"path": "output/report.pptx"})
    assert out.nodes[0]["source"] == "output/report.pptx"
    assert out.bindings_resolved == 1


def test_resolve_bindings_soft_misses_an_unresolvable_source_binding():
    """Tier 1: a source binding that misses is soft-skipped (present's
    universal miss semantics) — the resolved node simply lacks 'source',
    not a hard failure."""
    nodes = validate_blueprint({"component": "artifact", "source": {"$bind": "/absent"}})
    out = resolve_bindings(nodes, {})
    assert "source" not in out.nodes[0]
    assert out.bindings_dropped


def test_resolve_bindings_resolves_content_and_media_type():
    """Tier 1: content + media_type both resolve as literal text-family
    slots."""
    nodes = validate_blueprint(
        {"component": "artifact", "content": "a,b\n1,2\n", "media_type": "text/csv"},
    )
    out = resolve_bindings(nodes, {})
    assert out.nodes[0]["content"] == "a,b\n1,2\n"
    assert out.nodes[0]["media_type"] == "text/csv"


def test_resolve_bindings_never_touches_disk(tmp_path: Path):
    """Tier 2: resolve_bindings stays pure/I/O-free for the artifact
    branch — a source pointing at a path that does not exist on disk
    still resolves to that STRING without raising (no filesystem access
    happens here; only apply_artifact_resolution touches disk)."""
    nodes = validate_blueprint(
        {"component": "artifact", "source": str(tmp_path / "does-not-exist.txt")},
    )
    out = resolve_bindings(nodes, {})
    assert out.nodes[0]["source"] == str(tmp_path / "does-not-exist.txt")


# ── artifact_payload.apply_artifact_resolution ──────────────────────────


def test_apply_resolution_builds_a_reference_for_a_binary_source(tmp_path: Path):
    """Tier 2: a resolved 'source' node becomes the real OS-derived
    payload (PR-2's build_source_artifact_payload) — binary content
    becomes a Reference whose ref round-trips through PR-1's resolve_ref."""
    target = tmp_path / "image.png"
    target.write_bytes(b"\x89PNG" + b"\xff\xfe" * 5)
    nodes = [{"component": "artifact", "source": str(target)}]

    out = apply_artifact_resolution(nodes, tmp_path, "alice")

    assert out[0]["component"] == "artifact"
    assert out[0]["name"] == "image.png"
    ref = out[0]["body"]["ref"]
    assert resolve_ref(tmp_path, "alice", ref) == target.resolve()


def test_apply_resolution_builds_inline_for_content(tmp_path: Path):
    """Tier 2: a resolved 'content'+'media_type' node becomes an inline
    payload (PR-2's build_inline_artifact_payload) — no ref minted, no
    disk touched."""
    nodes = [{"component": "artifact", "content": "a,b\n1,2\n", "media_type": "text/csv"}]

    out = apply_artifact_resolution(nodes, tmp_path, "alice")

    assert out[0]["body"] == {"inline": "a,b\n1,2\n"}
    assert out[0]["media_type"] == "text/csv"
    assert "name" not in out[0]


def test_apply_resolution_marks_a_missing_source_as_an_error_not_a_crash(tmp_path: Path):
    """Tier 2: a source pointing at a file that doesn't exist becomes an
    explicit error marker, never a propagated exception through op
    execution — matches artifact_ref.resolve_ref's own missing-is-
    unresolvable shape."""
    nodes = [{"component": "artifact", "source": str(tmp_path / "gone.txt")}]

    out = apply_artifact_resolution(nodes, tmp_path, "alice")

    assert out[0] == {"component": "artifact", "error": "source_not_found"}


def test_apply_resolution_passes_through_a_soft_missed_artifact_node(tmp_path: Path):
    """Tier 2: an artifact node with neither source nor content resolved
    (both bindings soft-missed upstream) is left as a bare marker, not
    an error and not a crash."""
    nodes = [{"component": "artifact"}]
    out = apply_artifact_resolution(nodes, tmp_path, "alice")
    assert out == [{"component": "artifact"}]


def test_apply_resolution_leaves_non_artifact_nodes_completely_unchanged(tmp_path: Path):
    """Tier 2: (accept-side) a text/image/table node passes through
    apply_artifact_resolution byte-for-byte — this pass only touches
    'artifact' nodes."""
    nodes = [
        {"component": "text", "text": "hello"},
        {"component": "image", "src": "a.png", "alt": "x"},
    ]
    out = apply_artifact_resolution(nodes, tmp_path, "alice")
    assert out == nodes


# ── op_runtime/present.py: handle() end-to-end ───────────────────────────


def test_handle_resolves_a_real_artifact_end_to_end(tmp_path: Path):
    """Tier 2: the full path — a present op with an artifact blueprint,
    a real file under the op's own project_root, resolved through
    handle() into the final OS-derived payload on the rendered node."""
    target = tmp_path / "report.html"
    target.write_bytes(b"<h1>hi</h1>")
    ctx, events = _ctx(tmp_path)
    op = PresentIROp(
        kind="present",
        data_inline={},
        blueprint={"component": "artifact", "source": str(target)},
    )

    ack = _run(handle(op, ctx))

    assert ack["status"] == "ok"


def test_handle_end_to_end_reaches_the_renderer_with_the_built_payload(tmp_path: Path):
    """Tier 2: the RENDERED model (not just the ack) carries the final
    OS-derived artifact payload — confirms the wiring reaches the actual
    render surface, not just the ack path."""
    target = tmp_path / "report.html"
    target.write_bytes(b"<h1>hi</h1>")
    ctx, events = _ctx(tmp_path)

    class _RecordingRenderer:
        surface_name = "inline-cui"

        def __init__(self) -> None:
            self.rendered: list = []

        def render(self, resolved) -> None:
            self.rendered.append(resolved)

    renderer = _RecordingRenderer()
    ctx.presentation_renderer = renderer

    op = PresentIROp(
        kind="present",
        data_inline={},
        blueprint={"component": "artifact", "source": str(target)},
    )
    _run(handle(op, ctx))

    (resolved,) = renderer.rendered
    node = resolved.nodes[0]
    assert node["component"] == "artifact"
    assert node["media_type"] == "text/html"
    assert node["name"] == "report.html"
    # #4574 design C: a source-backed artifact's body always carries a ref
    # (the Art tab's openable route) alongside the small-file inline preview.
    assert node["body"]["inline"] == "<h1>hi</h1>"
    assert "ref" in node["body"]
