"""Tier 2: #3607 — the two chat-router op-context doors hand out the same thing.

A chat-router op can reach op_runtime through either of two entry points:

* ``Session._make_router_op_context`` — the session's own ``_file_op`` /
  ``_mcp_call_tool`` callbacks, and
* ``RouterHostAdapter.make_router_op_context`` — bound as
  ``RouterCallerState.op_context_factory``, i.e. the registry dispatch every
  LLM-emitted tool call goes through.

They used to assemble an OpContext each, from the same materials held twice
(``Session``'s attributes, and a 16-field copy of them injected into the
adapter). Which capabilities an op got therefore depended on which door it came
through, and twelve fields had already diverged. Both are now one call on one
``RouterOpContextSource``.

What is asserted here is the DEFECT, not the refactor: "the adapter takes one
parameter now" would be true of any parameter object. These arms compare what
the two doors PRODUCE, and check that a value which changes after the Session
is constructed is seen through both — a snapshot taken at construction time is
right on the first turn and wrong on every turn after it, which is the failure
mode a one-shot equality check cannot see.
"""
from __future__ import annotations

import asyncio
from dataclasses import fields, is_dataclass
from pathlib import Path

from reyn.core.op_runtime.context import OpContext
from tests._support.agent_session import make_session


def _fingerprint(value: object) -> object:
    """A comparable summary of an OpContext field.

    Identity is the wrong comparison for several fields: the intervention bus
    and the presentation renderer are BUILT per call, so two calls of the same
    door already return different objects. So: values compare by value,
    dataclasses recurse (a permission decl differing in one axis must show),
    and anything else compares by type — enough to catch "one door wires a
    renderer and the other wires None", which is the class of divergence #3607
    removed."""
    if value is None or isinstance(value, (str, int, float, bool, Path)):
        return value
    if isinstance(value, (list, tuple)):
        return [_fingerprint(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _fingerprint(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if is_dataclass(value) and not isinstance(value, type):
        return (
            type(value).__name__,
            {f.name: _fingerprint(getattr(value, f.name)) for f in fields(value)},
        )
    return type(value).__name__


def _shape(ctx: OpContext) -> dict:
    """Fingerprint EVERY declared OpContext field.

    Derived from the dataclass, not from a hand-listed set, so a field added to
    OpContext tomorrow is compared across both doors without editing this
    test — the way the twelve divergences accumulated was that nobody
    re-enumerated."""
    return {f.name: _fingerprint(getattr(ctx, f.name)) for f in fields(ctx)}


def _both_doors(session) -> tuple[dict, dict]:
    return (
        _shape(session._make_router_op_context()),
        _shape(session._router_host.make_router_op_context()),
    )


def _differing(a: dict, b: dict) -> list[str]:
    return sorted(k for k in a if a[k] != b[k])


def test_the_two_doors_build_the_same_op_context(tmp_path) -> None:
    """Tier 2: #3607 — every OpContext field is equal across both entry points.

    Before this landed, `agent_id`, `session_id`, `presentation_renderer`,
    `intervention_bus`, `presentation_registry`, `multimodal_config`,
    `compact_now`, `cancel_event` and `threat_scan` each reached one door and
    not the other, and `allowed_mcp` / `contextual_permission` /
    `sandbox_policy` were live on one door and frozen on the other."""
    session = make_session(agent_name="doors", workspace_state_dir=tmp_path)

    session_door, adapter_door = _both_doors(session)

    assert not _differing(session_door, adapter_door), (
        "the two chat-router op-context doors produced different OpContexts on "
        f"these fields: {_differing(session_door, adapter_door)} — an op's "
        "capabilities must not depend on which door it came through (#3607)"
    )


def test_the_comparison_can_tell_two_op_contexts_apart(tmp_path) -> None:
    """Tier 2: #3607 vacuity guard — the field comparison discriminates.

    The equality arm above is only as strong as `_fingerprint`. If it collapsed
    everything to a type name it would report agreement between two genuinely
    different contexts. Here the same door is asked twice across a real change,
    and the difference must show."""
    session = make_session(agent_name="discriminate", workspace_state_dir=tmp_path)

    before = _shape(session._make_router_op_context())
    session._allowed_mcp = ["only-this-server"]
    after = _shape(session._make_router_op_context())

    assert "permission_decl" in _differing(before, after), (
        "changing the MCP allowlist did not change the fingerprint — the "
        "comparison used by the equality arm cannot see field differences"
    )


def test_a_mid_session_mcp_narrowing_reaches_both_doors(tmp_path, monkeypatch) -> None:
    """Tier 2: #3607 — the per-agent MCP narrowing is not snapshotted.

    ``_reapply_per_agent_capability`` (the #2073 hot-reload seam) replaces the
    allowlist mid-session. Driven through that real seam with a real
    ``profile.yaml``: both doors must advertise the narrowed list afterwards.
    This is the stale-snapshot arm — a value captured when the Session was
    built passes a one-shot equality check and fails here."""
    project = tmp_path / "proj"
    agent_dir = project / ".reyn" / "agents" / "narrowed"
    agent_dir.mkdir(parents=True)
    # ``_hot_reload_project_root`` falls back to cwd when no registry root is
    # wired — that is the root the real seam re-reads profile.yaml from.
    monkeypatch.chdir(project)
    session = make_session(
        agent_name="narrowed",
        workspace_base_dir=project,
        workspace_state_dir=tmp_path / "state",
    )

    before_session, before_adapter = _both_doors(session)
    assert before_session["permission_decl"] == before_adapter["permission_decl"]

    (agent_dir / "profile.yaml").write_text("allowed_mcp:\n  - only-this-server\n")
    changed = asyncio.run(session._reapply_per_agent_capability({}))
    assert changed, "the hot-reload seam did not apply the profile's allowed_mcp"

    for door_name, ctx in (
        ("Session._make_router_op_context", session._make_router_op_context()),
        ("RouterHostAdapter.make_router_op_context",
         session._router_host.make_router_op_context()),
    ):
        assert ctx.permission_decl.allowed_mcp == ["only-this-server"], (
            f"{door_name} still advertises the pre-narrowing MCP allowlist "
            f"({ctx.permission_decl.allowed_mcp}) — a narrowing that reaches "
            "only one door is not a narrowing (#3607)"
        )


def test_the_turn_origin_of_the_current_turn_reaches_both_doors(tmp_path) -> None:
    """Tier 2: #3607 — turn provenance is read per build, not per session.

    ``_stamp_execution_context`` (proposal 0060 A7) reclassifies the turn on
    every turn, long after the Session is constructed. Both doors must report
    the CURRENT classification; the install-op provenance stamp (A9) is what
    reads it."""
    from reyn.runtime.turn_origin import TurnOrigin

    session = make_session(agent_name="origin", workspace_state_dir=tmp_path)

    session._stamp_execution_context(TurnOrigin.CLIENT_INPUT, {})
    for door in (session._make_router_op_context,
                 session._router_host.make_router_op_context):
        assert door().turn_origin == "user_directed"

    session._stamp_execution_context("pipeline_step", {})
    for door in (session._make_router_op_context,
                 session._router_host.make_router_op_context):
        assert door().turn_origin == "auto_improvement", (
            "a door still reports the previous turn's provenance — the value "
            "was snapshotted instead of supplied (#3607)"
        )


def test_the_agent_identity_reaches_the_registry_dispatch_door(tmp_path) -> None:
    """Tier 2: #3607 — the registry-dispatch door carries the agent identity.

    ``agent_id`` is the FP-0016 identity the MCP client sends as
    ``X-Reyn-Agent-Id``. It was ``None`` on this door and the real value on the
    Session door — #1412 flagged the asymmetry as a gap candidate and preserved
    it behaviorally, and the adapter had the value all along (it passes the
    same one to its own ``mcp_list_*`` gateway). Drift, not a decision: the two
    doors now agree."""
    session = make_session(agent_name="identity", workspace_state_dir=tmp_path)

    adapter_ctx = session._router_host.make_router_op_context()
    session_ctx = session._make_router_op_context()
    assert adapter_ctx.agent_id, (
        "the registry-dispatch door builds an OpContext with no agent identity "
        "— MCP calls made through it would send no X-Reyn-Agent-Id (#3607)"
    )
    assert adapter_ctx.agent_id == session_ctx.agent_id
