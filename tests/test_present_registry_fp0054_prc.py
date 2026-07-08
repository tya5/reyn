"""Present layer PR-C (FP-0054) — presentations.yaml registration + hot-reload + 4-stage fallback.

Contract + OS-invariant coverage for the named-template registry and the §3
template-source fallback chain (registered template → inline blueprint →
content-type default viewer → generic YAML/text). Real Session /
RouterHostAdapter / HotReloader / OpContext / EventLog — no collaborator mocks.
Assertions are on the public op ack, the resolved render model reaching a
recording renderer, the `presented` event payload, and the public registry
surface (`names()` / `has()` / `get()` / `session.presentation_registry` /
`router_host.get_presentation_registry()`) — never private state, never exact
render layout.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from reyn.config.loader import load_config
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.present import handle
from reyn.data.presentations.registry import (
    PresentationLoadError,
    PresentationRegistry,
    build_presentation_registry,
)
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.session import Session
from reyn.schemas.models import ALL_OP_KINDS, PresentIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

# A named template (operator config) whose value is a blueprint — the same
# declarative component tree an inline blueprint is.
_AUTHORS_TEMPLATE = [
    {
        "component": "table",
        "rows": {"$bind": "/results"},
        "columns": [{"header": "Author", "path": "/author"}],
    }
]


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False)


class _RecordingRenderer:
    """A real (non-mock) PresentationRenderer that records what reached the
    surface. Fire-and-continue: ``render`` returns nothing the op awaits.

    ``surface_name`` must be one of the guard's registered surfaces (#2670:
    ``get_neutralizer`` now fails closed on an unknown surface name rather than
    silently falling through to the terminal strategy) — ``"inline-cui"`` is a
    real registered surface, unlike the previous placeholder ``"test-surface"``.
    """

    surface_name = "inline-cui"

    def __init__(self) -> None:
        self.rendered: list = []

    def render(self, resolved) -> None:
        self.rendered.append(resolved)


def _ctx(
    tmp_path: Path,
    *,
    registry: "PresentationRegistry | None" = None,
    renderer: "_RecordingRenderer | None" = None,
) -> tuple[OpContext, EventLog]:
    events = EventLog()
    ws = Workspace(events=events, permission_resolver=_resolver(tmp_path))
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=_resolver(tmp_path),
        actor="present_prc_test",
        presentation_registry=registry,
        presentation_renderer=renderer,
    )
    return ctx, events


def _run(coro):
    return asyncio.run(coro)


def _all_leaf_text(resolved) -> str:
    """Every rendered string leaf in the render model (for a "did the data reach
    the surface" content-presence assertion — not a layout pin)."""
    parts: list[str] = []

    def _walk(obj) -> None:
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(resolved.nodes)
    return "\n".join(parts)


# ── Tier 1: config round-trip (non-default value reaches the registry) ────────


def test_config_roundtrip_named_template_reaches_registry(tmp_path: Path) -> None:
    """Tier 1: a real .reyn/config/presentations.yaml with a named template (a
    NON-default blueprint value) is loaded through the full config cascade + built
    into the registry under its entry name, with the blueprint structurally
    validated (mirrors the skills/pipelines disk round-trip)."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    cfg_dir = tmp_path / ".reyn" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "presentations.yaml").write_text(
        yaml.dump({"presentations": {"entries": {"authors": {"blueprint": _AUTHORS_TEMPLATE}}}}),
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)
    # The presentations section reached ReynConfig (the entries union-merge).
    assert cfg.presentations.get("entries", {}).get("authors", {}).get("blueprint") == _AUTHORS_TEMPLATE

    registry = build_presentation_registry(cfg.presentations)
    assert registry.has("authors")
    assert registry.names() == ["authors"]
    nodes = registry.get("authors")
    # Stored as the NORMALIZED validated node list (structure preserved).
    assert nodes[0]["component"] == "table"
    assert nodes[0]["columns"][0]["header"] == "Author"


def test_malformed_template_entry_skipped_not_fatal(tmp_path: Path) -> None:
    """Tier 1: a malformed blueprint entry (non-catalog component) is per-entry
    isolated — logged + skipped in lenient mode, the good sibling still loads;
    strict mode (the hot-reload posture) re-raises."""
    raw = {
        "entries": {
            "good": {"blueprint": _AUTHORS_TEMPLATE},
            "bad": {"blueprint": {"component": "button", "text": "click"}},
        }
    }
    registry = build_presentation_registry(raw)  # lenient default
    assert registry.has("good")
    assert not registry.has("bad")

    with pytest.raises(PresentationLoadError):
        build_presentation_registry(raw, strict=True)


def test_disabled_template_entry_not_registered(tmp_path: Path) -> None:
    """Tier 1: an entry with enabled: false is not registered (mirrors skills)."""
    raw = {"entries": {"off": {"blueprint": _AUTHORS_TEMPLATE, "enabled": False}}}
    registry = build_presentation_registry(raw)
    assert not registry.has("off")


# ── Tier 1: named-template resolution renders via the same binding path ───────


def test_registered_template_renders_via_same_binding_path_as_inline(tmp_path: Path) -> None:
    """Tier 1: a registered template resolves + renders via the SAME
    validate→resolve_bindings→render path an equivalent inline blueprint uses —
    the op ack reflects it identically and the render model reaches the surface."""
    data = {"results": [{"author": "amy"}, {"author": "bob"}]}
    registry = PresentationRegistry()
    registry.register("authors", build_presentation_registry(
        {"entries": {"authors": {"blueprint": _AUTHORS_TEMPLATE}}}).get("authors"))

    # Named-template path.
    r_named = _RecordingRenderer()
    ctx_n, _ = _ctx(tmp_path, registry=registry, renderer=r_named)
    ack_named = _run(handle(
        PresentIROp(kind="present", data_inline=data, view="authors"), ctx_n))

    # Equivalent inline-blueprint path over the same data.
    r_inline = _RecordingRenderer()
    ctx_i, _ = _ctx(tmp_path, registry=registry, renderer=r_inline)
    ack_inline = _run(handle(
        PresentIROp(kind="present", data_inline=data, blueprint=_AUTHORS_TEMPLATE), ctx_i))

    assert ack_named["ok"] is True
    assert ack_named["bindings_resolved"] == ack_inline["bindings_resolved"]
    assert ack_named["rows"] == ack_inline["rows"] == 2
    assert "note" not in ack_named  # no fallback — the template matched
    # The data reached the surface via the registered template.
    assert r_named.rendered, "the registered template must reach the wired renderer"
    surfaced = _all_leaf_text(r_named.rendered[-1])
    assert "amy" in surfaced and "bob" in surfaced


def test_registered_template_records_name_in_presented_event(tmp_path: Path) -> None:
    """Tier 1: the presented event records the REGISTERED NAME as its view
    field (not an inline-blueprint hash)."""
    data = {"results": [{"author": "amy"}]}
    registry = build_presentation_registry({"entries": {"authors": {"blueprint": _AUTHORS_TEMPLATE}}})
    ctx, events = _ctx(tmp_path, registry=registry, renderer=_RecordingRenderer())
    _run(handle(PresentIROp(kind="present", data_inline=data, view="authors"), ctx))
    ev = [e for e in events.all() if e.type == "presented"][-1]
    assert ev.data["view"] == "authors"
    assert ev.data["bindings_resolved"] >= 1


# ── Tier 1: 4-stage fallback (never a hard error; data reaches the surface) ───


def test_unknown_template_falls_to_generic_viewer_not_error(tmp_path: Path) -> None:
    """Tier 1: an UNKNOWN template name is NOT a hard error — it falls through the
    fallback chain to a generic viewer, the data still reaches the surface, and the
    ack carries ok=True + a note naming the fallback."""
    data = {"author": "amy", "title": "hello"}
    registry = build_presentation_registry({"entries": {"authors": {"blueprint": _AUTHORS_TEMPLATE}}})
    renderer = _RecordingRenderer()
    ctx, events = _ctx(tmp_path, registry=registry, renderer=renderer)

    ack = _run(handle(
        PresentIROp(kind="present", data_inline=data, view="does_not_exist"), ctx))

    assert ack["status"] == "ok"
    assert ack["ok"] is True
    assert "note" in ack and "not registered" in ack["note"]
    # The data reached the surface via the fallback (content-type default viewer:
    # a dict → keyvalue over its keys).
    assert renderer.rendered
    surfaced = _all_leaf_text(renderer.rendered[-1])
    assert "amy" in surfaced and "hello" in surfaced
    # And the presented event still fired (audit-first).
    assert [e for e in events.all() if e.type == "presented"]


def test_all_bindings_miss_template_falls_to_fallback_viewer(tmp_path: Path) -> None:
    """Tier 1: a registered template whose bindings ALL miss the data does NOT show
    an empty shell — it falls to the fallback viewer (data still reaches the user),
    while the ack preserves the LLM self-correction signal (all_bindings_missed +
    the drop list from the REQUESTED template) plus a fallback note."""
    # Template expects /results[*]/author; the data has no /results at all.
    registry = build_presentation_registry({"entries": {"authors": {"blueprint": _AUTHORS_TEMPLATE}}})
    data = {"completely": "different", "shape": [1, 2, 3]}
    renderer = _RecordingRenderer()
    ctx, _ = _ctx(tmp_path, registry=registry, renderer=renderer)

    ack = _run(handle(PresentIROp(kind="present", data_inline=data, view="authors"), ctx))

    assert ack["ok"] is True
    assert ack["all_bindings_missed"] is True  # the REQUESTED template's own outcome
    assert ack["bindings_resolved"] == 0
    assert "note" in ack and "all bindings missed" in ack["note"]
    # The data still reached the surface (a fallback viewer rendered it).
    assert renderer.rendered
    surfaced = _all_leaf_text(renderer.rendered[-1])
    assert "different" in surfaced


def test_inline_blueprint_all_miss_also_falls_back(tmp_path: Path) -> None:
    """Tier 1: the fallback applies to the stage-2 inline-blueprint path too — an
    LLM blueprint whose bindings all miss degrades to the fallback viewer rather
    than presenting an empty shell."""
    data = {"real_key": "real_value"}
    blueprint = [{"component": "text", "text": {"$bind": "/absent"}}]
    renderer = _RecordingRenderer()
    ctx, _ = _ctx(tmp_path, registry=None, renderer=renderer)

    ack = _run(handle(PresentIROp(kind="present", data_inline=data, blueprint=blueprint), ctx))

    assert ack["ok"] is True
    assert ack["all_bindings_missed"] is True
    assert "note" in ack
    surfaced = _all_leaf_text(renderer.rendered[-1])
    assert "real_value" in surfaced


def test_malformed_inline_blueprint_stays_hard_error_not_fallback(tmp_path: Path) -> None:
    """Tier 1: a malformed INLINE blueprint (non-catalog component) is a HARD error
    (status="error") — a template BUG, not a fallback trigger. Only unknown-name /
    all-miss route to the fallback chain."""
    ctx, _ = _ctx(tmp_path, registry=None, renderer=_RecordingRenderer())
    ack = _run(handle(PresentIROp(
        kind="present", data_inline={"a": 1},
        blueprint={"component": "button", "text": "click"}), ctx))
    assert ack["status"] == "error"
    assert ack["ok"] is False


def test_no_registry_wired_treats_named_template_as_unknown(tmp_path: Path) -> None:
    """Tier 1: with no registry wired (presentation_registry=None, direct/test
    construction), a named template is 'unknown' and falls to the generic viewer —
    never a crash on a None registry."""
    data = {"x": 1}
    renderer = _RecordingRenderer()
    ctx, _ = _ctx(tmp_path, registry=None, renderer=renderer)
    ack = _run(handle(PresentIROp(kind="present", data_inline=data, view="anything"), ctx))
    assert ack["ok"] is True
    assert "note" in ack
    assert "1" in _all_leaf_text(renderer.rendered[-1])


# ── Tier 1: operator/LLM boundary (registration is config, not an LLM action) ─


def test_referencing_a_template_name_never_registers_it(tmp_path: Path) -> None:
    """Tier 1: the write-gate boundary — the LLM authors inline blueprints only;
    naming a template in a `present` op is a READ-ONLY lookup that never registers.
    A present op referencing an unknown name leaves the registry unchanged (no
    self-registration side effect) — registration is an operator/config action."""
    registry = build_presentation_registry({"entries": {"authors": {"blueprint": _AUTHORS_TEMPLATE}}})
    assert not registry.has("llm_authored")
    ctx, _ = _ctx(tmp_path, registry=registry, renderer=_RecordingRenderer())

    _run(handle(PresentIROp(kind="present", data_inline={"a": 1}, view="llm_authored"), ctx))

    # The op did not register anything — the registry is exactly as the operator left it.
    assert not registry.has("llm_authored")
    assert registry.names() == ["authors"]


def test_no_present_register_op_kind_exists() -> None:
    """Tier 1: there is no op kind for the LLM to register a named template
    (unlike skill_install / pipeline_install, presentations are operator-config
    only — no install op). The only present-family op is `present` itself."""
    present_family = {k for k in ALL_OP_KINDS if "present" in k}
    assert present_family == {"present"}


# ── Tier 2: hot-reload — a new template is visible at the next turn boundary ──


def _make_session(tmp_path: Path) -> Session:
    """Minimal real Session whose presentation registry is built from the current
    config cascade (mirrors SessionFactoryConfig.from_config's build-once path)."""
    if not (tmp_path / "reyn.yaml").exists():
        (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    return Session(
        agent_name="prc-test",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        presentation_registry=build_presentation_registry(cfg.presentations),
    )


def _write_dynamic_template(tmp_path: Path, name: str, blueprint) -> None:
    path = tmp_path / ".reyn" / "config" / "presentations.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump({"presentations": {"entries": {name: {"blueprint": blueprint}}}}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_hotreload_seam_registered(tmp_path: Path) -> None:
    """Tier 2: the Session registers the presentations seam on the HotReloader."""
    session = _make_session(tmp_path)
    seam_names = [name for (name, _fn) in session._hot_reloader._seams]
    assert "presentations" in seam_names


@pytest.mark.asyncio
async def test_hotreload_adds_template_to_live_registry_dual_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: adding a template to .reyn/config/presentations.yaml + applying the
    presentations seam swaps the LIVE registry on BOTH session.presentation_registry
    AND router_host.get_presentation_registry() (the dual-write the adapter needs,
    since it holds its own captured copy) — the template is visible next turn."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    assert not session.presentation_registry.has("authors")

    _write_dynamic_template(tmp_path, "authors", _AUTHORS_TEMPLATE)
    changed = await session._reapply_presentations({})

    assert changed is True
    assert session.presentation_registry.has("authors")
    assert session.router_host.get_presentation_registry().has("authors"), (
        "the RouterHostAdapter's captured registry must reflect the swap (the "
        "dual-write the adapter needs since it never re-reads Session)"
    )


@pytest.mark.asyncio
async def test_hotreload_via_apply_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the template becomes visible through the SAME path /reload uses —
    HotReloader.request_reload + apply_pending — at the turn boundary."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    _write_dynamic_template(tmp_path, "authors", _AUTHORS_TEMPLATE)

    session._hot_reloader.request_reload(source="operator")
    summary = await session._hot_reloader.apply_pending()

    assert summary is not None
    assert "presentations" in summary["applied"]
    assert session.presentation_registry.has("authors")
    assert session.router_host.get_presentation_registry().has("authors")


@pytest.mark.asyncio
async def test_hotreload_malformed_template_keeps_old_registry_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a malformed template at reload time makes the seam return False and
    leaves the OLD registry (both holders) intact — atomic last-good (strict=True):
    the broken reload never half-applies or clears the live registry."""
    monkeypatch.chdir(tmp_path)
    _write_dynamic_template(tmp_path, "authors", _AUTHORS_TEMPLATE)
    session = _make_session(tmp_path)
    assert session.presentation_registry.has("authors")
    old_registry = session.presentation_registry

    # Break it: a non-catalog component fails the structural gate.
    _write_dynamic_template(tmp_path, "authors", {"component": "button", "text": "x"})
    changed = await session._reapply_presentations({})

    assert changed is False
    assert session.presentation_registry is old_registry, (
        "a failed rebuild must leave the Session's live registry object untouched"
    )
    assert session.router_host.get_presentation_registry() is old_registry
    assert session.presentation_registry.has("authors"), "the last-good template survives"


# ── Tier 2: no content bytes in the presented event on the fallback path ──────


def test_fallback_event_carries_no_content_bytes(tmp_path: Path) -> None:
    """Tier 2: even on the fallback path, the presented event payload carries refs
    + stats only — the surfaced data values never appear in the audit event."""
    secret = "UNIQUE_SENTINEL_PRC_7b21"
    registry = build_presentation_registry({"entries": {"authors": {"blueprint": _AUTHORS_TEMPLATE}}})
    ctx, events = _ctx(tmp_path, registry=registry, renderer=_RecordingRenderer())
    _run(handle(PresentIROp(
        kind="present", data_inline={"note": secret}, view="does_not_exist"), ctx))
    ev = [e for e in events.all() if e.type == "presented"][-1]
    assert secret not in json.dumps(ev.data)
