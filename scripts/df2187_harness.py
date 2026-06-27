"""#2187 dogfood-acceptance harness — multi-session run-to-quiescence A/B driver.

Builds a real AgentRegistry (config-loaded, like `reyn chat`), pre-spawns a worker
session, drives a lead session (gpt-oss-120b) to create + execute a PARENT task (the
ownership-execution context — the only context where child_settled / completion-join
fire), runs the run-loops to quiescence, and dumps the WAL/backend task-state.

A/B by codebase on the import path:
  - #2187:    PYTHONPATH=<e2e-coder>/src python scripts/df2187_harness.py --ws <dir>
  - baseline: PYTHONPATH=<user>/src      python scripts/df2187_harness.py --ws <dir>

Self-validate FIRST (lead mandate): confirm the harness drives the ownership chain
(execute-wake → child_settled → completion-join → lead DONE) on the #2187 arm before
taking the A/B differential — so a harness bug can't confound the efficacy result.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


async def _drive(ws: Path, prompt: str, *, worker_sid: str, timeout_s: float) -> dict:
    # Import inside the function so the import path (PYTHONPATH=arm/src) is honored.
    import os  # noqa: PLC0415

    from reyn.config import load_config, load_project_context  # noqa: PLC0415
    from reyn.core.events.state_log import StateLog  # noqa: PLC0415
    from reyn.llm.model_resolver import ModelResolver  # noqa: PLC0415
    from reyn.mcp.server import send_to_agent_impl  # noqa: PLC0415
    from reyn.runtime.factory_config import SessionFactoryConfig  # noqa: PLC0415
    from reyn.runtime.profile import AgentProfile  # noqa: PLC0415
    from reyn.runtime.registry import DEFAULT_AGENT_NAME, AgentRegistry  # noqa: PLC0415
    from reyn.runtime.scoped_session_factory import build_scoped_chat_session  # noqa: PLC0415
    from reyn.security.permissions.permissions import PermissionResolver  # noqa: PLC0415

    reyn_pkg = sys.modules["reyn"].__file__
    print(f"[harness] reyn import: {reyn_pkg}", file=sys.stderr)

    state_dir = ws / ".reyn"
    state_log = StateLog(state_dir / "state" / "wal.jsonl")
    config = load_config(ws)
    if config.api_base:
        os.environ.setdefault("LITELLM_API_BASE", config.api_base)
    resolver = ModelResolver(
        config.models, default_class=config.model,
        purpose_classes=config.model_class_by_purpose,
    )
    project_context = load_project_context(config, ws)
    perm_resolver = PermissionResolver(
        config_permissions=config.permissions,
        project_root=ws, file_zone_root=None, interactive=False,
        unsafe_python_allowed=False,
    )

    registry: AgentRegistry | None = None

    def _factory(profile: "AgentProfile"):
        return build_scoped_chat_session(
            agent_name=profile.name,
            model="standard",
            resolver=resolver,
            permission_resolver=perm_resolver,
            safety=config.safety,
            mcp_servers=[],
            output_language=None,
            prompt_cache_enabled=False,
            project_context=project_context,
            agent_role=profile.role,
            compaction_config=config.chat.compaction,
            reasoning_config=config.chat.reasoning,
            registry=registry,
            allowed_skills=profile.allowed_skills,
            allowed_mcp=profile.allowed_mcp,
            task_backend=registry.task_backend,
            events_config=config.events,
            state_log=state_log,
            budget_tracker=None,
            hooks_config=config.hooks,
            factory_config=SessionFactoryConfig.from_config(config),
            eager_embedding_build=False,
            agent_id=config.agent.id,
            exclude_tools=None,
            excluded_categories=frozenset(),
            contextual_permission=None,
            router_max_iterations=int(config.safety.loop.max_router_iterations),
            non_interactive=True,
            environment_backend=None,
            sandbox_backend=None,
            workspace_base_dir=None,
            workspace_state_dir=state_dir / "state",
        )

    registry = AgentRegistry(
        project_root=ws, session_factory=_factory, state_log=state_log,
        factory_config=SessionFactoryConfig.from_config(config),
    )
    name = DEFAULT_AGENT_NAME
    if not registry.exists(name):
        registry.create_agent(name)

    # Drive the lead with the bootstrap prompt; the run-loops then process the
    # decomposition / delegation / completion chain. Wait for quiescence.
    result = await asyncio.wait_for(
        send_to_agent_impl(registry, agent_name=name, message=prompt, timeout=timeout_s),
        timeout=timeout_s + 30,
    )
    # Let any woken sibling/worker run-loops drain.
    await asyncio.sleep(2.0)
    backend = registry.task_backend
    tasks = await backend.list()
    return {
        "reply_partial": result.get("partial"),
        "tasks": [
            {"id": t.task_id[:8], "name": t.name,
             "status": getattr(t.status, "value", t.status),
             "assignee": t.assignee, "requester": t.requester[:8] if t.requester else None,
             "requester_kind": getattr(t.requester_kind, "value", None)}
            for t in tasks
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", required=True)
    ap.add_argument("--worker-sid", default="worker-1")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--prompt", default=None)
    args = ap.parse_args()
    ws = Path(args.ws)
    prompt = args.prompt or (
        "task ツールで『短いレポートを作る』というタスクを1つ作成し、そのタスクに取り組んでください。"
        "取り組む際は2つのサブタスク（(1)要点を3つ挙げる (2)それを1段落にまとめる）に分解して"
        "それぞれ task として作成し、各サブタスクを完了させてから元のタスクを完了してください。"
    )
    out = asyncio.run(_drive(ws, prompt, worker_sid=args.worker_sid, timeout_s=args.timeout))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
