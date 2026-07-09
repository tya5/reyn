"""render_template op (FP-0055 PR-2) — sandboxed Jinja2 text-templating producer.

Contract + OS-invariant coverage for the `render_template` op: happy-path render,
strict/lenient undefined policy, SSTI sandbox containment (falsify), during-generate
resource caps (falsify), read-authority equivalence (`file.read` gate, both
directions), and the op's own canonical `_MAPPERS` entry (rendered string → `text`,
never a whole-dict `structured` fallback). Real Workspace + PermissionResolver +
EventLog — no collaborator mocks. Assertions are on the public op result, the
sandbox/cap behavior, and the canonical shape (never private state, never an exact
rendered-whitespace layout).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.offload.canonical import to_canonical
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.render_template import RenderTemplateBounds, handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import RenderTemplateIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


def _resolver(tmp_path: Path, config_permissions: dict | None = None) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config_permissions or {},
        project_root=tmp_path,
        interactive=False,
    )


def _ctx(
    tmp_path: Path,
    resolver: PermissionResolver,
    bounds: RenderTemplateBounds | None = None,
) -> tuple[OpContext, EventLog]:
    events = EventLog()
    ws = Workspace(events=events, permission_resolver=resolver)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        actor="render_template_test",
        render_template_bounds=bounds,
    )
    return ctx, events


def _run(coro):
    return asyncio.run(coro)


# ── Tier 1: render happy path ────────────────────────────────────────────────


def test_render_happy_path_inline(tmp_path):
    """Tier 1: inline template + inline data → the rendered string. Content-presence
    assertion (the interpolated + looped values appear), not an exact-whitespace pin."""
    ctx, _events = _ctx(tmp_path, _resolver(tmp_path))
    op = RenderTemplateIROp(
        kind="render_template",
        template="Report: {{ data.title }}\n{% for r in data.rows %}- {{ r }}\n{% endfor %}",
        data_inline={"title": "Q3", "rows": ["alpha", "beta"]},
    )
    result = _run(handle(op, ctx))
    assert result["status"] == "ok"
    assert result["truncated"] is False
    rendered = result["rendered"]
    assert "Report: Q3" in rendered
    assert "- alpha" in rendered
    assert "- beta" in rendered


def test_render_binds_data_ref_full_value(tmp_path, monkeypatch):
    """Tier 1: a data_ref is re-hydrated to its full value (same seam present uses)
    and bound under `data` — the template reaches nested fields of the on-disk JSON."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data.json").write_text(json.dumps({"results": [{"title": "hit-one"}]}))
    ctx, _events = _ctx(tmp_path, _resolver(tmp_path))
    op = RenderTemplateIROp(
        kind="render_template",
        template="{{ data.results[0].title }}",
        data_ref="data.json",
    )
    result = _run(handle(op, ctx))
    assert result["status"] == "ok"
    assert "hit-one" in result["rendered"]


# ── Tier 1: undefined policy ─────────────────────────────────────────────────


def test_strict_undefined_is_hard_error_naming_var(tmp_path):
    """Tier 1: strict (default) → an undefined variable is a hard error whose message
    names the missing name, so the LLM self-corrects. No silent blank render."""
    ctx, _events = _ctx(tmp_path, _resolver(tmp_path))
    op = RenderTemplateIROp(
        kind="render_template",
        template="Hello {{ data.name }}, ref {{ missing_var }}",
        data_inline={"name": "world"},
        undefined="strict",
    )
    result = _run(handle(op, ctx))
    assert result["status"] == "error"
    assert result["error_kind"] == "undefined"
    assert "missing_var" in result["error"]


def test_lenient_undefined_renders_empty_and_reports_meta(tmp_path):
    """Tier 1: lenient → undefined renders empty (no crash) and the referenced-but-
    unbound names surface in undefined_vars for self-correction."""
    ctx, _events = _ctx(tmp_path, _resolver(tmp_path))
    op = RenderTemplateIROp(
        kind="render_template",
        template="Hello {{ data.name }}[{{ missing_var }}]",
        data_inline={"name": "world"},
        undefined="lenient",
    )
    result = _run(handle(op, ctx))
    assert result["status"] == "ok"
    # undefined rendered empty → the bracket is empty, the bound value present.
    assert "Hello world[]" in result["rendered"]
    assert "missing_var" in result["undefined_vars"]


# ── Tier 1: SSTI containment (falsify) ───────────────────────────────────────


def test_ssti_attribute_traversal_is_blocked(tmp_path):
    """Tier 1: falsify — a template attempting an SSTI attribute-traversal escape
    (`().__class__.__bases__`) is stopped by the SandboxedEnvironment — it does NOT
    execute; the op returns a structured security error and the class object never
    reaches the output."""
    ctx, _events = _ctx(tmp_path, _resolver(tmp_path))
    op = RenderTemplateIROp(
        kind="render_template",
        template="{{ ().__class__.__bases__[0].__subclasses__() }}",
        data_inline={},
    )
    result = _run(handle(op, ctx))
    assert result["status"] == "error"
    assert result["error_kind"] == "security"
    # Falsify: the escape did not succeed — no rendered string of subclasses.
    assert "rendered" not in result


# ── Tier 1: resource bounds (falsify) ────────────────────────────────────────


def test_output_size_cap_truncates_during_generate(tmp_path):
    """Tier 1: falsify — a template that would generate huge output is capped DURING
    generate — the result is truncated + flagged in meta, NOT an OOM/hang. The size
    guard fires and the output is bounded by the injected budget."""
    bounds = RenderTemplateBounds(max_output_chars=50, wall_clock_seconds=100.0)
    ctx, _events = _ctx(tmp_path, _resolver(tmp_path), bounds=bounds)
    op = RenderTemplateIROp(
        kind="render_template",
        template="{% for i in range(100000) %}X{% endfor %}",
        data_inline={},
    )
    result = _run(handle(op, ctx))
    assert result["status"] == "ok"
    assert result["truncated"] is True
    assert result["truncate_reason"] == "max_output_chars"
    # Bounded by the injected budget (compared to the budget var, not a format pin).
    assert len(result["rendered"]) <= bounds.max_output_chars
    # It is the runaway loop's output, cut short — nothing but the loop body char.
    assert set(result["rendered"]) <= {"X"}


def test_wall_clock_backstop_fires(tmp_path):
    """Tier 1: falsify — the wall-clock backstop bounds a generator independently of
    size — with a zero- /negative-time budget it truncates on the first streamed chunk
    (deterministic: any elapsed time exceeds it), flagging the wall-clock reason."""
    bounds = RenderTemplateBounds(max_output_chars=10_000_000, wall_clock_seconds=-1.0)
    ctx, _events = _ctx(tmp_path, _resolver(tmp_path), bounds=bounds)
    op = RenderTemplateIROp(
        kind="render_template",
        template="{% for i in range(100000) %}line{{ i }}\n{% endfor %}",
        data_inline={},
    )
    result = _run(handle(op, ctx))
    assert result["status"] == "ok"
    assert result["truncated"] is True
    assert result["truncate_reason"] == "wall_clock_seconds"


def test_small_render_is_not_truncated(tmp_path):
    """Tier 1: falsify direction — a small render under the default bounds is NOT
    flagged truncated — so the cap tests above are not a blanket 'always truncated'."""
    ctx, _events = _ctx(tmp_path, _resolver(tmp_path))
    op = RenderTemplateIROp(
        kind="render_template", template="{{ data.x }}", data_inline={"x": "small"},
    )
    result = _run(handle(op, ctx))
    assert result["status"] == "ok"
    assert result["truncated"] is False
    assert "truncate_reason" not in result


# ── Tier 1: template syntax error ────────────────────────────────────────────


def test_template_syntax_error_is_hard_error(tmp_path):
    """Tier 1: a malformed template is a hard error (never masked as a blank render)."""
    ctx, _events = _ctx(tmp_path, _resolver(tmp_path))
    op = RenderTemplateIROp(
        kind="render_template", template="{% for x in %}", data_inline={},
    )
    result = _run(handle(op, ctx))
    assert result["status"] == "error"
    assert result["error_kind"] == "template_error"


# ── Tier 1: read-authority equivalence (denied ⇔ file.read denied) ───────────


def test_template_ref_denied_iff_file_read_denied(tmp_path, monkeypatch):
    """Tier 1: a template_ref read gate is identical to file.read — a config
    file.read:deny denies BOTH. Real resolver (not None); falsify (allow) below."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tpl.j2").write_text("{{ data.x }}")
    (tmp_path / "data.json").write_text(json.dumps({"x": 1}))
    deny = _resolver(tmp_path, {"file": {"read": "deny"}})
    ctx, _events = _ctx(tmp_path, deny)
    op = RenderTemplateIROp(
        kind="render_template", template_ref="tpl.j2", data_inline={"x": 1},
    )
    result = _run(execute_op(op, ctx))
    assert result["status"] == "denied"
    # file.read denied on the same path (equivalence).
    with pytest.raises(PermissionError):
        _run(deny.require_file_read(PermissionDecl(), str(tmp_path / "tpl.j2"), "t"))


def test_data_ref_denied_iff_file_read_denied(tmp_path, monkeypatch):
    """Tier 1: a data_ref read gate is identical to file.read (same equivalence as the
    template_ref path — both route through the one gate seam)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data.json").write_text(json.dumps({"x": 1}))
    deny = _resolver(tmp_path, {"file": {"read": "deny"}})
    ctx, _events = _ctx(tmp_path, deny)
    op = RenderTemplateIROp(
        kind="render_template", template="{{ data.x }}", data_ref="data.json",
    )
    result = _run(execute_op(op, ctx))
    assert result["status"] == "denied"


def test_refs_allowed_when_file_read_allowed(tmp_path, monkeypatch):
    """Tier 1: falsify direction — with read allowed (default CWD zone) the same
    template_ref + data_ref render ok — so the denials above are not a blanket refusal."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tpl.j2").write_text("value={{ data.x }}")
    (tmp_path / "data.json").write_text(json.dumps({"x": 42}))
    allow = _resolver(tmp_path)
    ctx, _events = _ctx(tmp_path, allow)
    op = RenderTemplateIROp(
        kind="render_template", template_ref="tpl.j2", data_ref="data.json",
    )
    result = _run(execute_op(op, ctx))
    assert result["status"] == "ok"
    assert "value=42" in result["rendered"]


# ── Tier 1: render_template's own canonical mapper (guards the FP-0056 gap) ───


def test_canonical_mapper_rendered_string_becomes_text():
    """Tier 1: the op's own result → canonical `text` (the rendered string), NOT a
    whole-dict `structured` attachment. Guards against re-introducing the FP-0056
    whole-dict fallback for this new producer kind."""
    result = {
        "kind": "render_template", "status": "ok", "ok": True,
        "rendered": "the rendered body", "truncated": False,
    }
    canonical = to_canonical(result, source="render_template")
    assert canonical["text"] == "the rendered body"
    # NOT the fallback: no structured attachment carrying the whole dict.
    assert canonical.get("attachments") == []


def test_canonical_mapper_surfaces_truncated_and_undefined_meta():
    """Tier 1: truncated + undefined_vars are carried as signal meta (the LLM's
    self-correction channel), and an error result surfaces its message with isError."""
    truncated = to_canonical({
        "kind": "render_template", "status": "ok", "ok": True,
        "rendered": "XXX", "truncated": True, "truncate_reason": "max_output_chars",
        "undefined_vars": ["missing_var"],
    }, source="render_template")
    assert truncated["meta"]["truncated"] is True
    assert truncated["meta"]["truncate_reason"] == "max_output_chars"
    assert "missing_var" in truncated["meta"]["undefined_vars"]

    errored = to_canonical({
        "kind": "render_template", "status": "error", "ok": False,
        "error_kind": "undefined", "error": "'missing_var' is undefined",
    }, source="render_template")
    assert errored["meta"]["isError"] is True
    assert "missing_var" in errored["text"]


# ── Tier 1: producer-neutrality (raw output, no producer-side escaping) ──────


def test_producer_returns_raw_unneutralized_output(tmp_path):
    """Tier 1: the producer returns RAW rendered bytes — a control/ESC sequence in the
    data survives in the result (neutralization is the SINK's job, not the producer's).
    Falsify against producer-side stripping."""
    ctx, _events = _ctx(tmp_path, _resolver(tmp_path))
    esc = "\x1b[31mred\x1b[0m"
    op = RenderTemplateIROp(
        kind="render_template", template="{{ data.msg }}", data_inline={"msg": esc},
    )
    result = _run(handle(op, ctx))
    assert result["status"] == "ok"
    # Raw ESC bytes retained — the producer did not strip/escape them.
    assert "\x1b[31m" in result["rendered"]
