"""Tier 2c: operator `render_template:` yaml bounds reach the render_template op
THROUGH a real Session.

#2679 wired the operator config `render_template.max_output_chars` /
`.wall_clock_seconds` through `ReynConfig` → `Session` → the router `OpContext`
(both `make_router_op_context` twins) → the `render_template` op's during-generate
cap. The op already had safe defaults + an `OpContext.render_template_bounds`
override seam (FP-0055 PR-2); this adds the missing operator-facing config.

The point of #2679 is the **Session→op link** — the config value only matters if a
real Session threads it into the OpContext the op actually reads. So these drive a
REAL `Session` (built from a real `load_config`) and exercise BOTH Session-side
builders that feed the op in production:

  - ``Session._make_router_op_context`` (the file/MCP-op twin), and
  - ``Session._router_host.make_router_op_context`` (the RouterHostAdapter twin
    bound as ``op_context_factory`` in ``tools/types.py`` — the path the
    ``render_template`` tool actually dispatches through on chat + pipeline).

Stripping the ``render_template_bounds`` threading from EITHER Session-side builder
makes the matching assertion below go RED (cp-falsify verified), so the reachability
is CI-enforced, not merely reviewed once. Real objects throughout (no mocks);
assertions on the public op result (`truncated` / `truncate_reason`), never private
state or an exact rendered layout.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.config import RenderTemplateConfig
from reyn.config.chat import _build_render_template_config
from reyn.config.loader import load_config
from reyn.core.op_runtime.render_template import handle
from reyn.runtime.session import Session


class _RenderOp:
    """A minimal render_template op double: a 1000-char pure-computation render
    (inline template, no template_ref / data_ref, so no file.read gate is
    exercised — this test is about the bounds wiring, not read-authority)."""

    kind = "render_template"
    template = "{% for i in range(1000) %}X{% endfor %}"
    template_ref = None
    data_ref = None
    data_inline: dict = {}
    undefined = "strict"


def _run_op_via(builder) -> dict:
    """Build an OpContext via ``builder`` and run the real render_template op on it."""
    ctx = builder()
    return asyncio.run(handle(_RenderOp(), ctx))


def test_operator_yaml_bounds_cap_the_op_through_a_real_session(tmp_path: Path) -> None:
    """Tier 2c: a NON-DEFAULT `render_template:` yaml value round-trips through
    `load_config` into a real `Session`, and BOTH Session-side OpContext builders
    hand the op a cap that truncates a 1000-char render.

    This is the #2679 guard: it drives the real Session→OpContext→op link (not a
    hand-assembled bounds), so removing the `render_template_bounds` threading from
    either `Session._make_router_op_context` or the RouterHostAdapter twin fails
    here (cp-falsify verified) — the previously silent "plumbed-but-not-threaded"
    regression is now caught.
    """
    (tmp_path / "reyn.yaml").write_text(
        "model: standard\n"
        "render_template:\n"
        "  max_output_chars: 20\n"
        "  wall_clock_seconds: 1.5\n"
    )

    # yaml → ReynConfig round-trip (the non-default value survives the loader).
    cfg = load_config(cwd=tmp_path)
    assert cfg.render_template.max_output_chars == 20
    assert cfg.render_template.wall_clock_seconds == 1.5

    # The production shape: the frontend factories pass `config.render_template`
    # into the Session (registry_bootstrap / chat.py). A real Session resolves it
    # into the bounds it threads into every router OpContext builder.
    session = Session(agent_name="t", render_template_config=cfg.render_template)

    # Builder 1 — Session._make_router_op_context (file/MCP twin).
    r_session = _run_op_via(session._make_router_op_context)
    assert r_session["status"] == "ok"
    assert r_session["truncated"] is True
    assert r_session["truncate_reason"] == "max_output_chars"

    # Builder 2 — RouterHostAdapter.make_router_op_context (the op_context_factory
    # the render_template tool actually dispatches through on chat + pipeline).
    r_host = _run_op_via(session._router_host.make_router_op_context)
    assert r_host["status"] == "ok"
    assert r_host["truncated"] is True
    assert r_host["truncate_reason"] == "max_output_chars"


def test_default_config_leaves_normal_render_uncapped_through_a_real_session(
    tmp_path: Path,
) -> None:
    """Tier 2c: with no `render_template:` section the Session gets the safe
    defaults (256_000 / 5.0), so a normal-size render is NOT truncated through
    either builder — the operator config is opt-in, default behaviour unchanged.
    """
    # The loader's absent-section path yields the safe defaults (hermetic — no
    # machine-global config I/O).
    default_cfg = _build_render_template_config(None)
    assert default_cfg == RenderTemplateConfig()
    assert default_cfg.max_output_chars == 256_000
    assert default_cfg.wall_clock_seconds == 5.0

    session = Session(agent_name="t", render_template_config=default_cfg)

    for builder in (
        session._make_router_op_context,
        session._router_host.make_router_op_context,
    ):
        result = _run_op_via(builder)
        assert result["status"] == "ok"
        assert result["truncated"] is False
        assert "truncate_reason" not in result
        # The full render is present (existence, not an exact-size pin).
        assert len(result["rendered"]) > 0
