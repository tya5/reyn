"""Present op (FP-0054 PR-A) — declarative model + binding + guard + read-authority.

Contract + OS-invariant coverage for the `present` op against a null renderer
(no UI surface in PR-A). Real Workspace + PermissionResolver + EventLog — no
collaborator mocks. Assertions are on the public op ack, the resolved-binding
model, and the `presented` event payload (never private state, never exact
render layout — there is no renderer yet).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.present import handle
from reyn.core.present import (
    PresentBlueprintError,
    resolve_bindings,
    resolve_pointer,
    validate_blueprint,
)
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import PresentIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


def _resolver(tmp_path: Path, config_permissions: dict | None = None) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config_permissions or {},
        project_root=tmp_path,
        interactive=False,
    )


def _ctx(tmp_path: Path, resolver: PermissionResolver) -> tuple[OpContext, EventLog]:
    events = EventLog()
    ws = Workspace(events=events, permission_resolver=resolver)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        actor="present_test",
    )
    return ctx, events


def _run(coro):
    return asyncio.run(coro)


# ── Tier 1: binding resolution ───────────────────────────────────────────────


def test_binding_hit_binds_value():
    """Tier 1: a path hit binds the value into the rendered leaf."""
    nodes = validate_blueprint({"component": "text", "text": {"$bind": "/title"}})
    out = resolve_bindings(nodes, {"title": "hello world"})
    assert out.bindings_resolved == 1
    assert out.bindings_dropped == []
    assert out.nodes[0]["text"] == "hello world"


def test_binding_miss_soft_skips_and_records_path_not_found():
    """Tier 1: a path miss soft-skips the binding + records path_not_found — never
    a hard failure."""
    nodes = validate_blueprint({"component": "text", "text": {"$bind": "/absent"}})
    out = resolve_bindings(nodes, {"title": "x"})
    assert out.bindings_resolved == 0
    assert {"path": "/absent", "reason": "path_not_found"} in out.bindings_dropped
    # soft-skip: the leaf is simply absent, no exception, other structure intact.
    assert out.nodes[0]["component"] == "text"


def test_scalar_into_table_coerces_to_one_row_type_mismatch():
    """Tier 1: a scalar bound into a table rows slot coerces to a 1-row table +
    records type_mismatch (the §4 coercion rule)."""
    nodes = validate_blueprint({
        "component": "table",
        "rows": {"$bind": "/x"},
        "columns": [],
    })
    out = resolve_bindings(nodes, {"x": "just-a-scalar"})
    assert out.rows == 1
    assert {"path": "/x", "reason": "type_mismatch"} in out.bindings_dropped


def test_row_relative_column_paths_resolve_per_row():
    """Tier 1: table column paths resolve row-relative (RFC 6901 relative to each
    iterated row); a column that misses on ALL rows records one path_not_found."""
    nodes = validate_blueprint({
        "component": "table",
        "rows": {"$bind": "/results"},
        "columns": [
            {"header": "Title", "path": "/title"},
            {"header": "Author", "path": "/author"},
        ],
    })
    data = {"results": [{"title": "A", "author": "amy"}, {"title": "B", "author": "bob"}]}
    out = resolve_bindings(nodes, data)
    cols = {c["header"]: c["cells"] for c in out.nodes[0]["columns"]}
    assert cols["Title"] == ["A", "B"]
    assert cols["Author"] == ["amy", "bob"]
    # No column missed → no path_not_found drops.
    assert all(d["reason"] != "path_not_found" for d in out.bindings_dropped)


def test_column_missing_on_all_rows_records_path_not_found():
    """Tier 1: a column path absent from every row records exactly one
    path_not_found (template/data shape mismatch), not one-per-row noise."""
    nodes = validate_blueprint({
        "component": "table",
        "rows": {"$bind": "/results"},
        "columns": [{"header": "Author", "path": "/author"}],
    })
    data = {"results": [{"title": "A"}, {"title": "B"}]}
    out = resolve_bindings(nodes, data)
    drops = [d for d in out.bindings_dropped if d["reason"] == "path_not_found"]
    assert drops == [{"path": "/author", "reason": "path_not_found"}]


def test_all_bindings_missed_outcome_exposed():
    """Tier 1: when every binding misses, all_bindings_missed is True (the
    generic-viewer fallback signal); still no hard failure."""
    nodes = validate_blueprint([
        {"component": "text", "text": {"$bind": "/nope1"}},
        {"component": "text", "text": {"$bind": "/nope2"}},
    ])
    out = resolve_bindings(nodes, {"real": 1})
    assert out.bindings_resolved == 0
    assert out.all_bindings_missed is True


def test_literal_only_blueprint_is_not_all_missed():
    """Tier 1: a blueprint with no bindings (literals only) never reports
    all_bindings_missed (there were no bindings to miss)."""
    nodes = validate_blueprint({"component": "text", "text": "a literal heading"})
    out = resolve_bindings(nodes, {})
    assert out.all_bindings_missed is False


def test_resolve_pointer_rfc6901_whole_doc_and_escapes():
    """Tier 1: JSON Pointer resolution — '' is the whole document; '~1'/'~0'
    decode to '/'/'~'; out-of-range/absent → not found."""
    doc = {"a/b": {"c~d": 7}, "arr": [10, 20]}
    assert resolve_pointer(doc, "") == (doc, True)
    assert resolve_pointer(doc, "/a~1b/c~0d") == (7, True)
    assert resolve_pointer(doc, "/arr/1") == (20, True)
    assert resolve_pointer(doc, "/arr/9")[1] is False
    assert resolve_pointer(doc, "/missing")[1] is False


# ── Tier 1: ack shape (drop reason categories) ───────────────────────────────


def test_ack_shape_reports_drops_with_reason_categories(tmp_path):
    """Tier 1: the op ack carries {ok, bindings_resolved, bindings_dropped, rows}
    with each drop as {path, reason} in the three reason categories."""
    ctx, _events = _ctx(tmp_path, _resolver(tmp_path))
    op = PresentIROp(
        kind="present",
        data_inline={"results": [{"title": "A"}], "big": {"nested": "obj"}},
        blueprint=[
            {"component": "table", "rows": {"$bind": "/results"},
             "columns": [{"header": "Missing", "path": "/author"}]},
            {"component": "text", "text": {"$bind": "/big"}},  # dict into text → type_mismatch
        ],
    )
    ack = _run(handle(op, ctx))
    assert ack["ok"] is True
    assert set(ack.keys()) >= {"ok", "bindings_resolved", "bindings_dropped", "rows"}
    reasons = {d["reason"] for d in ack["bindings_dropped"]}
    assert "path_not_found" in reasons
    assert "type_mismatch" in reasons
    for drop in ack["bindings_dropped"]:
        assert set(drop.keys()) == {"path", "reason"}
        assert drop["reason"] in {"path_not_found", "type_mismatch", "guard_stripped"}


# ── Tier 1: blueprint structural gate ────────────────────────────────────────


def test_non_catalog_component_rejected():
    """Tier 1: a component outside the display-only catalog is a hard rejection."""
    with pytest.raises(PresentBlueprintError):
        validate_blueprint({"component": "button", "text": "click me"})


def test_non_path_binding_rejected():
    """Tier 1: a binding whose value is not a JSON-Pointer string is rejected
    (bindings are path expressions only — no smuggled objects/expressions)."""
    with pytest.raises(PresentBlueprintError):
        validate_blueprint({"component": "text", "text": {"$bind": {"expr": "1+1"}}})
    with pytest.raises(PresentBlueprintError):
        validate_blueprint({"component": "text", "text": {"$bind": "not-a-pointer"}})


def test_unknown_slot_rejected():
    """Tier 1: a slot not in a component's allowed set is rejected (tight surface)."""
    with pytest.raises(PresentBlueprintError):
        validate_blueprint({"component": "text", "onclick": "evil()"})


# ── Tier 1: presentation-guard (escape survival, FP-0051 idiom) ──────────────


def test_terminal_escape_in_data_is_neutralized_not_rendered():
    """Tier 1: a terminal escape sequence in bound data is neutralized (never
    reaches the rendered leaf) + the binding is recorded guard_stripped.

    FP-0051 idiom: the literal control byte in the data must not survive into the
    surface-bound value."""
    nodes = validate_blueprint({"component": "text", "text": {"$bind": "/v"}})
    out = resolve_bindings(nodes, {"v": "safe\x1b[31mINJECT\x1b[0m"})
    rendered = out.nodes[0]["text"]
    assert "\x1b" not in rendered           # the ESC control byte is gone
    assert "INJECT" in rendered             # the human-readable text survives (inert)
    assert {"path": "/v", "reason": "guard_stripped"} in out.bindings_dropped


def test_rich_markup_in_data_is_escaped_literal():
    """Tier 1: Rich markup in bound data is escaped so it renders literally (the
    bracket survives as text, the styling does not drive the surface)."""
    nodes = validate_blueprint({"component": "text", "text": {"$bind": "/v"}})
    out = resolve_bindings(nodes, {"v": "[bold red]owned[/bold red]"})
    rendered = out.nodes[0]["text"]
    # The literal bracket text is preserved but escaped (backslash-guarded), so
    # Rich will not interpret it as a style tag.
    assert "bold red" in rendered
    assert "\\[" in rendered
    assert {"path": "/v", "reason": "guard_stripped"} in out.bindings_dropped


def test_root_pointer_into_text_is_size_capped():
    """Tier 1: a `/` (root) pointer bound into a text component is size-capped
    (the whole-file-dump guard) + recorded guard_stripped."""
    from reyn.core.present.guard import MAX_LEAF_CHARS

    nodes = validate_blueprint({"component": "text", "text": {"$bind": ""}})
    huge = "x" * (MAX_LEAF_CHARS + 5000)
    out = resolve_bindings(nodes, huge)
    assert len(out.nodes[0]["text"]) < len(huge)
    assert {"path": "", "reason": "guard_stripped"} in out.bindings_dropped


def test_labels_neutralized_at_render_seam():
    """Tier 1: literal labels (kv labels / column headers) are neutralized through
    the single render seam (not at parse). The structural gate keeps them raw;
    resolve_bindings neutralizes every render-leaf including labels."""
    raw = "name\x1b[31m"
    nodes = validate_blueprint({
        "component": "keyvalue",
        "rows": [{"label": raw, "value": "v"}],
    })
    # Structural gate is purely structural — the label is still raw here.
    assert nodes[0]["rows"][0]["label"] == raw
    # The single seam neutralizes it in the render model.
    out = resolve_bindings(nodes, {})
    assert "\x1b" not in out.nodes[0]["rows"][0]["label"]


def test_literal_text_slot_value_is_neutralized_via_single_seam():
    """Tier 1: an LLM-authored LITERAL (non-$bind) escape / Rich-markup in a
    text-family slot is neutralized + reported guard_stripped — the same standard
    as a bound value (single-seam: no literal bypasses the guard). RED against the
    pre-unification code where text-slot literals were only size-capped."""
    nodes = validate_blueprint(
        {"component": "text", "text": "safe\x1b[31mINJECT\x1b[0m [bold]owned[/bold]"}
    )
    out = resolve_bindings(nodes, {})
    rendered = out.nodes[0]["text"]
    assert "\x1b" not in rendered          # ESC control byte gone
    assert "INJECT" in rendered            # readable text survives (inert)
    assert "\\[" in rendered               # Rich markup escaped literal
    assert any(d["reason"] == "guard_stripped" for d in out.bindings_dropped)


def test_terminal_neutralizer_does_not_html_escape_code_content():
    """Tier 1: the terminal neutralizer does NOT HTML-escape — a `<div>` in a
    `code`/`diff` slot survives literally (no entity-escape corruption). HTML
    neutralization is a future web renderer's job, not the terminal's."""
    # Bound value into a code slot.
    nodes = validate_blueprint({"component": "code", "text": {"$bind": "/src"}})
    out = resolve_bindings(nodes, {"src": "<div class='x'>y & z</div>"})
    rendered = out.nodes[0]["text"]
    assert rendered == "<div class='x'>y & z</div>"   # byte-for-byte survival
    assert "&lt;" not in rendered and "&amp;" not in rendered
    # No terminal-dangerous content → not stripped.
    assert all(d["reason"] != "guard_stripped" for d in out.bindings_dropped)

    # Same for a literal in a code slot.
    nodes2 = validate_blueprint({"component": "code", "text": "<p>&nbsp;</p>"})
    out2 = resolve_bindings(nodes2, {})
    assert out2.nodes[0]["text"] == "<p>&nbsp;</p>"


# ── Tier 1: presented event field presence + OS-computed ingested ────────────


def test_presented_event_carries_required_fields(tmp_path):
    """Tier 1: the presented (P6) event carries the required audit fields incl.
    surface + OS-computed ingested + the drop list."""
    ctx, events = _ctx(tmp_path, _resolver(tmp_path))
    op = PresentIROp(
        kind="present", data_inline={"a": 1},
        blueprint={"component": "text", "text": {"$bind": "/a"}},
    )
    _run(handle(op, ctx))
    ev = [e for e in events.all() if e.type == "presented"]
    assert ev, "present emitted no presented event"
    d = ev[-1].data
    for field in ("data_ref", "template", "surface", "ingested",
                  "bindings_resolved", "bindings_dropped", "rows"):
        assert field in d
    assert d["ingested"] in {"none", "partial", "full"}


def test_ingested_is_os_computed_from_prior_read(tmp_path, monkeypatch):
    """Tier 1: ingested is OS-computed from the events log — a prior full read_file
    on the ref → 'full'; no prior read → 'none'. Never LLM-self-reported."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ref.json").write_text(json.dumps({"a": 1}))
    ctx, events = _ctx(tmp_path, _resolver(tmp_path))

    op = PresentIROp(
        kind="present", data_ref="ref.json",
        blueprint={"component": "text", "text": {"$bind": "/a"}},
    )
    _run(handle(op, ctx))
    first = [e for e in events.all() if e.type == "presented"][-1]
    assert first.data["ingested"] == "none"

    # Simulate a prior full read of the ref, then present again → 'full'.
    events.emit("tool_executed", op="read_file", path="ref.json")
    _run(handle(op, ctx))
    second = [e for e in events.all() if e.type == "presented"][-1]
    assert second.data["ingested"] == "full"


def test_ingested_partial_on_truncated_read(tmp_path, monkeypatch):
    """Tier 1: only a truncated prior read on the ref → 'partial'."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ref.json").write_text(json.dumps({"a": 1}))
    ctx, events = _ctx(tmp_path, _resolver(tmp_path))
    events.emit("tool_executed", op="read_file", path="ref.json", truncated=True)
    op = PresentIROp(
        kind="present", data_ref="ref.json",
        blueprint={"component": "text", "text": {"$bind": "/a"}},
    )
    _run(handle(op, ctx))
    ev = [e for e in events.all() if e.type == "presented"][-1]
    assert ev.data["ingested"] == "partial"


# ── Tier 1: read-authority equivalence (present denied ⇔ file.read denied) ────


def test_present_denied_iff_file_read_denied(tmp_path, monkeypatch):
    """Tier 1: present's data_ref read gate is identical to file.read — a config
    file.read:deny denies BOTH. Real resolver (not None); falsify direction below."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ref.json").write_text(json.dumps({"a": 1}))
    deny = _resolver(tmp_path, {"file": {"read": "deny"}})
    ctx, _events = _ctx(tmp_path, deny)

    op = PresentIROp(
        kind="present", data_ref="ref.json",
        blueprint={"component": "text", "text": {"$bind": "/a"}},
    )
    # present denied
    ack = _run(execute_op(op, ctx))
    assert ack["status"] == "denied"
    # file.read denied on the same path (equivalence)
    with pytest.raises(PermissionError):
        _run(deny.require_file_read(PermissionDecl(), str(tmp_path / "ref.json"), "present_test"))


def test_present_allowed_when_file_read_allowed(tmp_path, monkeypatch):
    """Tier 1: falsify direction — with read allowed (default CWD zone), present
    resolves the ref and returns ok, so the deny above is not a blanket refusal."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ref.json").write_text(json.dumps({"a": 42}))
    allow = _resolver(tmp_path)
    ctx, _events = _ctx(tmp_path, allow)

    op = PresentIROp(
        kind="present", data_ref="ref.json",
        blueprint={"component": "text", "text": {"$bind": "/a"}},
    )
    # file.read allowed on the same path
    _run(allow.require_file_read(PermissionDecl(), str(tmp_path / "ref.json"), "present_test"))
    ack = _run(execute_op(op, ctx))
    assert ack["status"] == "ok"
    assert ack["ok"] is True
    assert ack["bindings_resolved"] == 1


# ── Tier 2: fire-and-continue + no content bytes in the event ────────────────


def test_fire_and_continue_does_not_pause_the_run(tmp_path):
    """Tier 2: present is fire-and-continue — unlike ask_user it needs NO
    intervention_bus and returns a result without pausing (a pausing op would
    raise without a bus)."""
    resolver = _resolver(tmp_path)
    events = EventLog()
    ws = Workspace(events=events, permission_resolver=resolver)
    # intervention_bus deliberately left None (the ask_user pause dependency).
    ctx = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="present_test", intervention_bus=None,
    )
    op = PresentIROp(
        kind="present", data_inline={"a": 1},
        blueprint={"component": "text", "text": {"$bind": "/a"}},
    )
    ack = _run(handle(op, ctx))
    assert ack["status"] == "ok"


def test_event_carries_no_content_bytes(tmp_path):
    """Tier 2: the presented event payload carries refs + stats only — the bound
    data values never appear in the audit event (data is durable in the ref)."""
    ctx, events = _ctx(tmp_path, _resolver(tmp_path))
    secret_value = "UNIQUE_SENTINEL_CONTENT_9f3a"
    op = PresentIROp(
        kind="present", data_inline={"a": secret_value},
        blueprint={"component": "text", "text": {"$bind": "/a"}},
    )
    _run(handle(op, ctx))
    ev = [e for e in events.all() if e.type == "presented"][-1]
    serialized = json.dumps(ev.data)
    assert secret_value not in serialized
