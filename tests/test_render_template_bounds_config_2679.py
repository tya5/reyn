"""Tier 2c: operator `render_template:` yaml bounds reach the render_template op.

#2679 wired the operator config `render_template.max_output_chars` /
`.wall_clock_seconds` through `ReynConfig` → `Session` → the router `OpContext`
(both `make_router_op_context` twins) → the `render_template` op's during-generate
cap. The op already had safe defaults + an `OpContext.render_template_bounds`
override seam (FP-0055 PR-2); this adds the missing operator-facing config.

These pin the two load-bearing links the config adds, with REAL objects (no
mocks): (1) the yaml→ReynConfig round-trip with a NON-DEFAULT value, and (2) the
bounds threaded through the real `build_router_op_context` actually cap the real
op. Assertions are on the public op result (`truncated` / `truncate_reason`) and
public config fields — never private state, never an exact rendered layout.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.config.chat import _build_render_template_config
from reyn.config.loader import load_config
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.render_template import RenderTemplateBounds, handle
from reyn.runtime.router_op_context import build_router_op_context
from reyn.security.permissions.permissions import PermissionResolver


class _RenderOp:
    """A minimal render_template op double: a 1000-char pure-computation render
    (no template_ref / data_ref, so no file.read gate is exercised — this test is
    about the bounds wiring, not read-authority)."""

    kind = "render_template"
    template = "{% for i in range(1000) %}X{% endfor %}"
    template_ref = None
    data_ref = None
    data_inline: dict = {}
    undefined = "strict"


def _ctx_with_bounds(tmp_path: Path, bounds: "RenderTemplateBounds | None"):
    """Build a real router OpContext carrying ``bounds`` via the production
    single-source builder (the same factory both router hosts call)."""
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    return build_router_op_context(
        events=EventLog(),
        permission_resolver=resolver,
        file_permissions=None,
        mcp_servers=None,
        mcp_servers_flat=[],
        allowed_mcp=None,
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path,
        environment_backend=None,
        sandbox_backend=None,
        sandbox_policy=None,
        agent_id=None,
        presentation_renderer=None,
        render_template_bounds=bounds,
    )


def test_operator_yaml_bounds_cap_the_render_template_op(tmp_path: Path) -> None:
    """Tier 2c: a NON-DEFAULT `render_template:` yaml value round-trips to
    ReynConfig and, threaded through the real router OpContext builder, caps the
    real op — a 1000-char render is truncated at the operator's tiny bound.
    """
    (tmp_path / "reyn.yaml").write_text(
        "model: standard\n"
        "render_template:\n"
        "  max_output_chars: 20\n"
        "  wall_clock_seconds: 1.5\n"
    )

    # (1) yaml → ReynConfig round-trip (the non-default value survives the loader).
    cfg = load_config(cwd=tmp_path)
    assert cfg.render_template.max_output_chars == 20
    assert cfg.render_template.wall_clock_seconds == 1.5

    # (2) config → RenderTemplateBounds (the exact conversion Session.__init__ does)
    #     → the real builder → the real op honours the operator's tiny cap.
    bounds = RenderTemplateBounds(
        max_output_chars=cfg.render_template.max_output_chars,
        wall_clock_seconds=cfg.render_template.wall_clock_seconds,
    )
    ctx = _ctx_with_bounds(tmp_path, bounds)
    capped = asyncio.run(handle(_RenderOp(), ctx))

    assert capped["status"] == "ok"
    assert capped["truncated"] is True
    assert capped["truncate_reason"] == "max_output_chars"

    # Behavioural contrast: the SAME render under a generous bound is NOT truncated
    # and is strictly longer — proving the operator's cap actually shortened the
    # output (not merely flipped a flag). No exact-size pin (Tier-4 format pinning).
    generous_ctx = _ctx_with_bounds(
        tmp_path, RenderTemplateBounds(max_output_chars=256_000, wall_clock_seconds=5.0),
    )
    full = asyncio.run(handle(_RenderOp(), generous_ctx))
    assert full["truncated"] is False
    assert len(capped["rendered"]) < len(full["rendered"])


def test_absent_render_template_section_leaves_normal_output_uncapped(
    tmp_path: Path,
) -> None:
    """Tier 2c: with no `render_template:` section the loader yields the safe
    defaults (256_000 / 5.0), so a normal-size render is NOT truncated — the
    operator config is opt-in and default behaviour is unchanged.
    """
    # The loader's absent-section path (hermetic — no machine-global config I/O).
    default_cfg = _build_render_template_config(None)
    assert default_cfg.max_output_chars == 256_000
    assert default_cfg.wall_clock_seconds == 5.0

    bounds = RenderTemplateBounds(
        max_output_chars=default_cfg.max_output_chars,
        wall_clock_seconds=default_cfg.wall_clock_seconds,
    )
    ctx = _ctx_with_bounds(tmp_path, bounds)
    result = asyncio.run(handle(_RenderOp(), ctx))

    assert result["status"] == "ok"
    # A normal-size render passes through un-truncated under the generous default.
    assert result["truncated"] is False
    assert "truncate_reason" not in result
    # The full render is present (existence, not an exact-size pin).
    assert len(result["rendered"]) > 0
