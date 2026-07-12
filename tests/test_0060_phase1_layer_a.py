"""Tier 2: OS invariant — proposal 0060 Phase 1 Layer A (A7 turn_origin seam,
A9 uniform provenance stamp, A8 presentation_install op).

Co-vet pins (docs/deep-dives/proposals/0060-llm-wielding-foundation.md,
Addendum B — settled design record):

  1. **Provenance is (2)B-structural** (mirrors emit_hook_event's ctx-side
     kind-construction falsify): the presentation_install op schema has NO
     provenance field; the handler stamps ONLY from ctx.turn_origin. Falsify:
     an auto_improvement-provenance ctx + a handler reading a spoofed op-level
     value would let the LLM self-declare user_directed — the REAL handler
     must not do this (asserted directly: PresentationInstallIROp has no
     provenance field to spoof, AND the written entry always equals
     ctx.turn_origin regardless of anything on the op).
  2. **turn_origin completeness is fail-safe**: every turn kind
     Session.run_one_iteration dispatches maps through
     _stamp_execution_context; an unmapped/new kind resolves to
     "auto_improvement", NEVER "user_directed". Falsify: introduce a brand-new
     kind with no dedicated mapping entry → if it resolved to "user_directed"
     this test goes RED (fail-safe broken).
  3. **present-install threat gate is validate_blueprint**: a malformed /
     non-catalog blueprint is refused before any config mutation. Falsify:
     strip the validate_blueprint call from the install path → a malformed
     blueprint installs → RED.

Real PermissionResolver + StateLog + OpContext + a real Session throughout
(no mocks) — mirrors test_skill_install_pr_c.py / test_pipeline_install.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.presentation_install import handle as presentation_install_handle
from reyn.core.present import PresentBlueprintError, validate_blueprint
from reyn.runtime.session import Session
from reyn.schemas.models import PresentationInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from tests._support.router_host_adapter import make_adapter

# ── shared stubs (real API surface, no mocks) ─────────────────────────────────


class _StubWorkspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


class _Events:
    """Minimal real-callable event log stub — passes emit calls through without side effects."""
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


def _make_ctx(
    tmp_path: Path, *, turn_origin: "str | None", state_log: StateLog | None = None,
) -> OpContext:
    """A real OpContext with a PermissionResolver that allows presentations.yaml
    writes, and a caller-chosen turn_origin (the field under test — mirrors the
    OS-set value ``_stamp_execution_context`` would have produced for the turn)."""
    config_path = tmp_path / ".reyn" / "config" / "presentations.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    resolver.session_approve_path(str(config_path), "test", "file.write")

    decl = PermissionDecl(
        file_write=[{"path": str(config_path), "scope": "just_path"}],
    )
    return OpContext(
        workspace=_StubWorkspace(base_dir=tmp_path),
        events=_Events(),
        permission_decl=decl,
        permission_resolver=resolver,
        actor="test",
        intervention_bus=None,
        subscribers=[],
        state_log=state_log,
        turn_origin=turn_origin,
    )


_VALID_BLUEPRINT = {"component": "text", "text": "hello"}


# ── Test 1: (2)B-structural — no op-level provenance field to spoof ───────────


def test_presentation_install_op_schema_has_no_provenance_field():
    """Tier 2: (2)B-structural — PresentationInstallIROp carries NO provenance
    field. Falsify: if a future edit adds ``provenance`` to the schema, an LLM
    could set it directly and this assertion goes RED (the schema must stay
    provenance-field-free; the value's authority lives ONLY in ctx.turn_origin,
    A9)."""
    op = PresentationInstallIROp(
        kind="presentation_install", name="x", blueprint=_VALID_BLUEPRINT,
    )
    assert "provenance" not in type(op).model_fields


@pytest.mark.asyncio
async def test_presentation_install_stamps_provenance_from_ctx_only_not_op(tmp_path):
    """Tier 2: (2)B-structural falsify — the handler stamps entry["provenance"]
    from ctx.turn_origin ALONE. Simulates the spoof attempt: an op instance
    carries no provenance field at all (confirmed above), so even a
    maximally-adversarial op construction cannot influence the written value —
    only ctx does. Two ctx values (one per provenance) → two installs → the
    written entries differ ONLY by ctx.turn_origin, proving the value's sole
    source. RED if the handler ever reads anything but ctx.turn_origin (e.g. a
    stray op.provenance / op.extra kwarg smuggled through)."""
    config_path = tmp_path / ".reyn" / "config" / "presentations.yaml"

    ctx_auto = _make_ctx(tmp_path, turn_origin="auto_improvement")
    op = PresentationInstallIROp(
        kind="presentation_install", name="card_a", blueprint=_VALID_BLUEPRINT,
    )
    result_auto = await presentation_install_handle(op, ctx_auto)
    assert result_auto["status"] == "installed"

    ctx_user = _make_ctx(tmp_path, turn_origin="user_directed")
    op2 = PresentationInstallIROp(
        kind="presentation_install", name="card_b", blueprint=_VALID_BLUEPRINT,
    )
    result_user = await presentation_install_handle(op2, ctx_user)
    assert result_user["status"] == "installed"

    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    entries = written["presentations"]["entries"]
    assert entries["card_a"]["provenance"] == "auto_improvement"
    assert entries["card_b"]["provenance"] == "user_directed"


# ── Test 2: turn_origin completeness fail-safe ────────────────────────────────


def test_turn_origin_maps_every_known_kind_and_fails_safe_on_unmapped(tmp_path):
    """Tier 2: A7 fail-safe — ONLY an explicit kind=="user" turn grants
    "user_directed"; every other known kind (hook / pipeline_result / the wake
    family / agent_request / agent_response) AND a brand-new, never-registered
    kind all resolve to "auto_improvement". Asserts on the PUBLIC op-ctx output
    (current adapter's turn_origin), not private state — mirrors the sibling
    _stamp_execution_context / current_task_id completeness test
    (test_2107_B15_preserve_self_continuation_1953.py). FALSIFY: if an unmapped
    kind fell through to "user_directed", this test goes RED (a Phase-4-gate
    bypass — 0060 SS2.7)."""
    s = Session(agent_name="alice", state_log=StateLog(tmp_path / "wal.jsonl"))
    adapter = make_adapter(
        agent_name="alice", turn_origin_fn=lambda: s._current_turn_origin,
    )

    def current() -> "str | None":
        return adapter.make_router_op_context().turn_origin

    # The ONLY kind granting user_directed.
    s._stamp_execution_context("user", {})
    assert current() == "user_directed"

    # Every other DISPATCHED kind (hook self-continuation, sub-agent turns, the
    # wake family, an async pipeline's terminal result) resolves to the
    # stricter auto_improvement — including kinds that PRESERVE current_task_id
    # (hook/agent_response): turn_origin has its OWN (simpler) fail-safe rule,
    # not derived from the task-ownership PRESERVE/RESET bands.
    for kind in (
        "hook", "agent_response", "agent_request", "pipeline_result",
        "task_ready", "task_dependency_aborted",
        # a brand-new kind this method has never seen before.
        "some_unknown_future_kind_0060",
    ):
        s._stamp_execution_context(kind, {"meta": {}})
        assert current() == "auto_improvement", (
            f"kind={kind!r} must fail-safe to auto_improvement, never silently "
            "default to user_directed"
        )

    # Re-arming with "user" and then a fresh unmapped kind still fails safe
    # (not "sticky user" — every turn is reclassified from scratch).
    s._stamp_execution_context("user", {})
    assert current() == "user_directed"
    s._stamp_execution_context("brand_new_kind_never_seen", {})
    assert current() == "auto_improvement"


# ── Test 3: present-install threat gate is validate_blueprint ────────────────


@pytest.mark.asyncio
async def test_malformed_blueprint_is_blocked_before_any_config_write(tmp_path):
    """Tier 2: A8 — validate_blueprint IS the structural threat gate for
    presentation_install (no scan_for_threats call — there is no free-text
    field). A non-catalog component is refused BEFORE any config mutation:
    status="blocked", no presentations.yaml written at all. FALSIFY: if the
    handler's validate_blueprint call were stripped, the malformed blueprint
    below would install successfully → RED."""
    config_path = tmp_path / ".reyn" / "config" / "presentations.yaml"
    ctx = _make_ctx(tmp_path, turn_origin="user_directed")

    # Sanity: this blueprint really is rejected by the structural gate itself
    # (confirms the fixture is a true negative, not an accidentally-valid one).
    with pytest.raises(PresentBlueprintError):
        validate_blueprint({"component": "not_in_catalog", "text": "hi"})

    op = PresentationInstallIROp(
        kind="presentation_install",
        name="evil",
        blueprint={"component": "not_in_catalog", "text": "hi"},
    )
    result = await presentation_install_handle(op, ctx)
    assert result["status"] == "blocked"
    assert not config_path.exists()


@pytest.mark.asyncio
async def test_valid_blueprint_installs_and_registry_accepts_it(tmp_path):
    """Tier 2: positive control for test 3 — a catalog-valid blueprint installs
    (status="installed") and the written presentations.yaml entry is itself
    accepted by build_presentation_registry (the same structural gate the
    op-install path and the config-load path both run through)."""
    from reyn.data.presentations.registry import build_presentation_registry

    ctx = _make_ctx(tmp_path, turn_origin="user_directed")
    op = PresentationInstallIROp(
        kind="presentation_install", name="hello_card", blueprint=_VALID_BLUEPRINT,
    )
    result = await presentation_install_handle(op, ctx)
    assert result["status"] == "installed"
    assert result["name"] == "hello_card"

    config_path = tmp_path / ".reyn" / "config" / "presentations.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    registry = build_presentation_registry(raw["presentations"], strict=True)
    assert registry.has("hello_card")
