"""Tier 1: FP-0056 issue #2681 Bucket C — status-text canonical mappers for the 26 status-only
producers (write/ack/spawn-ack results, no readable body).

Before this burn-down each of these producers was ``CANONICAL_TODO`` — the provisional whole-dict
fallback (``text=""``, the whole result as a ``structured`` attachment). ``make_status_text_mapper``
(``core/offload/canonical.py``) is the ONE reusable factory every one of them now declares through: a
short human/LLM-readable status line + the same structured fields carried as ``meta`` — behavior-
preserving (nothing the caller could read via the whole-dict fallback is lost, only reshaped).

Real result dicts (the shapes the producers actually emit — read from source, not invented), no
mocks. Spot-checks a representative producer per family (memory / cron / spawn / install / drop) via
the LIVE registered declaration (``to_canonical(result, source=<name>)``), not the bare mapper
function — so the assertions exercise the real registration seam, not just the factory in isolation.
"""
from __future__ import annotations

import pytest

# Importing op_runtime eagerly registers every op handler + its canonical declaration; building the
# default tool registry declares every ToolDefinition's — both are needed for ``to_canonical``'s
# ``source=`` resolution to find our new declarations (mirrors the coverage-gate test's setup).
import reyn.core.op_runtime as _op_runtime  # noqa: F401
from reyn.core.offload.canonical import make_status_text_mapper, to_canonical
from reyn.tools import get_default_registry

get_default_registry()


# ─────────────────────────────────────────────────────────────────────────────
# Spot-checks — representative real result shapes, across memory / cron / spawn / install / drop
# ─────────────────────────────────────────────────────────────────────────────


def test_memory_remember_shared_renders_status_text_with_meta() -> None:
    """Tier 1: ``remember_shared`` (tools/memory.py ``_handle_remember`` success shape) renders a
    short status line naming what was saved + where, with ``layer``/``path`` carried as meta."""
    result = {"saved": "user_role", "layer": "shared", "path": ".reyn/memory/user_role.md"}
    canonical = to_canonical(result, source="remember_shared")
    assert "user_role" in canonical["text"]
    assert ".reyn/memory/user_role.md" in canonical["text"]
    assert canonical["attachments"] == []
    assert canonical["meta"]["layer"] == "shared"
    assert canonical["meta"]["path"] == ".reyn/memory/user_role.md"


def test_memory_forget_memory_renders_status_text() -> None:
    """Tier 1: ``forget_memory`` success shape (``{deleted, layer}``) renders a short deletion line."""
    result = {"deleted": "feedback_tone", "layer": "agent"}
    canonical = to_canonical(result, source="forget_memory")
    assert "feedback_tone" in canonical["text"]
    assert canonical["meta"]["layer"] == "agent"


def test_cron_register_renders_replaced_vs_registered() -> None:
    """Tier 1: ``cron_register`` (tools/cron.py) distinguishes a fresh register from a replace, and
    carries the live-update/path signal as meta."""
    fresh = {
        "status": "ok", "name": "morning_news", "replaced": False,
        "live_update_applied": True, "path": ".reyn/config/cron.yaml",
    }
    canonical = to_canonical(fresh, source="cron_register")
    assert "Registered" in canonical["text"]
    assert "morning_news" in canonical["text"]
    assert canonical["meta"]["live_update_applied"] is True

    replaced = {**fresh, "replaced": True}
    canonical_replaced = to_canonical(replaced, source="cron_register")
    assert "Replaced" in canonical_replaced["text"]


def test_agent_spawn_renders_status_text_with_note() -> None:
    """Tier 1: ``agent_spawn`` (tools/agent_spawn.py ``spawn_agent`` success shape) names the new
    agent + its parent, with the OS-authored ``note`` appended verbatim (lossless)."""
    result = {
        "status": "spawned", "name": "researcher", "parent": "lead",
        "note": "New agent created; its capabilities are capped at ⊆ yours.",
    }
    canonical = to_canonical(result, source="agent_spawn")
    assert "researcher" in canonical["text"]
    assert "lead" in canonical["text"]
    assert "capped at" in canonical["text"]  # the note rides along, not dropped
    assert canonical["meta"]["parent"] == "lead"


def test_session_spawn_renders_status_text() -> None:
    """Tier 1: ``session_spawn`` (tools/session_spawn.py) success shape names the sid + mode."""
    result = {
        "status": "spawned", "sid": "sess-42", "mode": "ephemeral",
        "note": "Fresh session spawned + task submitted; it runs in isolation.",
    }
    canonical = to_canonical(result, source="session_spawn")
    assert "sess-42" in canonical["text"]
    assert "ephemeral" in canonical["text"]
    assert canonical["meta"]["sid"] == "sess-42"


def test_delegate_to_agent_renders_status_text() -> None:
    """Tier 1: ``delegate_to_agent`` (tools/delegate_to_agent.py) dispatch-ack shape."""
    result = {"status": "dispatched", "to": "peer_agent", "note": "Peer's reply will arrive later."}
    canonical = to_canonical(result, source="delegate_to_agent")
    assert "peer_agent" in canonical["text"]
    assert canonical["meta"]["to"] == "peer_agent"


def test_index_drop_shared_by_op_kind_and_drop_source_tool() -> None:
    """Tier 1: ``index_drop`` (op kind) and ``drop_source`` (its tool wrapper, tools/drop_source.py)
    surface the SAME ``{removed, chunks_dropped}`` handler result — both declared through the same
    mapper, so both render identically."""
    result = {"removed": True, "chunks_dropped": 17}
    for source in ("index_drop", "drop_source"):
        canonical = to_canonical(result, source=source)
        assert "17" in canonical["text"]
        assert "chunk" in canonical["text"]
        assert canonical["meta"]["chunks_dropped"] == 17
        assert canonical["meta"]["removed"] is True


def test_skill_install_local_renders_status_text() -> None:
    """Tier 1: ``skill_install_local`` surfaces ``op_runtime.skill_install.handle``'s
    ``{status:"installed", name, path, ...}`` result verbatim (post dispatch-envelope unwrap)."""
    result = {
        "status": "installed", "name": "code_review", "path": "/skills/code_review",
        "description": "Reviews a diff.", "config_path": ".reyn/config/skills.yaml", "source": "",
    }
    canonical = to_canonical(result, source="skill_install_local")
    assert "code_review" in canonical["text"]
    assert canonical["meta"]["path"] == "/skills/code_review"
    assert canonical["meta"]["config_path"] == ".reyn/config/skills.yaml"


def test_mcp_install_registry_renders_ok_and_needs_secrets() -> None:
    """Tier 1: ``mcp_install_registry`` surfaces ``op_runtime.mcp_install.handle``'s TWO success
    sub-shapes — the install-complete ack and the ``needs_secrets`` short-circuit (whose ``guide`` IS
    the actionable message, so it becomes ``text`` verbatim)."""
    ok_result = {
        "kind": "mcp_install", "status": "ok", "server_id": "io.example/server-time",
        "server_name": "server-time", "scope": "local", "installed_path": "/mcp/server-time",
        "runtime": "npx", "env_keys_set": [], "source": "",
    }
    canonical = to_canonical(ok_result, source="mcp_install_registry")
    assert "server-time" in canonical["text"]
    assert canonical["meta"]["scope"] == "local"

    needs_secrets = {
        "kind": "mcp_install", "status": "needs_secrets", "server_id": "io.example/server-time",
        "missing_secret_keys": ["API_KEY"],
        "guide": "Server requires secret env-vars not yet set: API_KEY.",
    }
    canonical_secrets = to_canonical(needs_secrets, source="mcp_install_registry")
    assert canonical_secrets["text"] == needs_secrets["guide"]
    assert canonical_secrets["meta"]["missing_secret_keys"] == ["API_KEY"]


def test_mcp_subscribe_resource_renders_status_text() -> None:
    """Tier 1: ``subscribe_mcp_resource`` surfaces the ``mcp_subscribe_resource`` op kind's
    ``{kind, status:"ok", server, uri}`` result verbatim."""
    result = {"kind": "mcp_subscribe_resource", "status": "ok", "server": "docs", "uri": "docs://readme"}
    canonical = to_canonical(result, source="subscribe_mcp_resource")
    assert "docs://readme" in canonical["text"]
    assert "docs" in canonical["text"]
    assert canonical["meta"]["server"] == "docs"


# ─────────────────────────────────────────────────────────────────────────────
# Error shape still routes through the shared error seam (piece #1) — unaffected by the new mappers
# ─────────────────────────────────────────────────────────────────────────────


def test_error_shape_bypasses_the_new_status_mapper() -> None:
    """Tier 1: an error-shaped result (carries ``error``) is intercepted by the shared error seam
    BEFORE any of the new status-text mappers run — the mapper only ever sees a success dict."""
    error_result = {"status": "error", "kind": "agent_exists", "error": "agent 'x' already exists."}
    canonical = to_canonical(error_result, source="agent_spawn")
    assert canonical["meta"]["isError"] is True
    assert "already exists" in canonical["text"]


# ─────────────────────────────────────────────────────────────────────────────
# Falsify — the factory itself: without a declared mapper the result takes the lossless whole-dict
# fallback (empty text, the whole dict as a structured attachment) — proving the assertions above
# actually exercise the new mapper, not a vacuously-passing shape.
# ─────────────────────────────────────────────────────────────────────────────


def test_falsify_undeclared_source_takes_whole_dict_fallback_not_status_text() -> None:
    """Tier 1: (falsify) the SAME result shape through an UNDECLARED source name takes the whole-dict
    fallback (empty ``text``, the dict in ``attachments``) — confirming the status-text rendering
    above is actually produced by the declared mapper, not some source-independent default."""
    result = {"saved": "user_role", "layer": "shared", "path": ".reyn/memory/user_role.md"}
    fallback = to_canonical(result, source="_not_a_registered_producer_2681")
    assert fallback["text"] == ""
    assert fallback["attachments"] == [{"kind": "structured", "data": result}]


def test_make_status_text_mapper_meta_keys_skip_absent_fields() -> None:
    """Tier 1: a ``meta_keys`` entry absent from a given result shape is silently skipped (not set to
    ``None``) — lets one factory call cover a producer with more than one success sub-shape (e.g.
    ``mcp_install``'s ``ok`` vs ``needs_secrets``, exercised above)."""
    mapper = make_status_text_mapper(render=lambda r: "did the thing", meta_keys=("a", "b"))
    canonical = mapper({"a": 1})
    assert canonical["meta"] == {"a": 1}
    assert "b" not in canonical["meta"]


def test_make_status_text_mapper_empty_render_gets_explicit_marker() -> None:
    """Tier 1: a ``render`` that returns an empty/blank string is NOT surfaced as blank ``text`` (which
    would spuriously fire the ``canonical_degraded`` invariant, FP-0056 v2 piece #2) — it gets the
    factory's explicit marker instead."""
    mapper = make_status_text_mapper(render=lambda r: "", empty_marker="(nothing happened)")
    canonical = mapper({})
    assert canonical["text"] == "(nothing happened)"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
