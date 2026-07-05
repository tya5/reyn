"""#2093: the shared session-factory config bundle — completeness-by-construction.

The five session-factory sites (cli/chat, cli/dogfood, web/deps, chainlit, cli/mcp)
each thread a set of UNIFORM, reyn.yaml-config-derived args identically into
``build_scoped_chat_session`` (8) and ``AgentRegistry`` (3). Historically a new uniform
arg had to be added at all five sites by hand — and was silently missed at one
(``sandbox_config`` at the A2A factory; ``delegation_capability_default`` at
``mcp.py``).

``SessionFactoryConfig.from_config`` is the SINGLE point that maps ``ReynConfig`` →
those uniform args. Every site builds the bundle once and passes it to both consumers,
which read their part. A new uniform arg is added in ONE place (the dataclass +
``from_config``) and reaches all five sites — type-enforced, so it cannot be missed.

This is deliberately ONLY the uniform config-derived args. Per-SITE args that
legitimately differ (model / resolver / state_log / the env+sandbox backends /
workspace dirs / contextual_permission / agent_id / allowed_mcp / task_backend /
router_max_iterations / non_interactive / eager_embedding_build) stay explicit
per-site params — they are NOT a drift class.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionFactoryConfig:
    """The uniform, config-derived session-factory args (see module docstring)."""

    # ── build_scoped_chat_session uniform config (8) ────────────────────────
    sandbox_config: Any
    multimodal_config: Any
    action_retrieval_config: Any
    embedding_config: Any
    router_config: Any
    retry_config: Any
    chat_tool_use_scheme: str
    # #2548 PR-A: enabled skill registry snapshot (list[SkillEntry]), built from
    # config.skills. Uniform config-derived arg → reaches all factory sites.
    available_skills: Any
    # #2575: the populated PipelineRegistry, built ONCE per frontend from
    # config.pipelines (disk scan → parse → register). Threaded to every Session
    # (incl. spawns, which reuse this bundle) so the pipelines dir is parsed once
    # per session tree, not re-globbed per session — mirrors the build-once
    # available_skills snapshot. Empty registry when project_root is unknown
    # (direct/test from_config(config)) → byte-identical to pre-#2575.
    pipeline_registry: Any
    # ── AgentRegistry uniform config (3) ────────────────────────────────────
    delegation_capability_default: str
    # #2103 C3: operator spawn-tree bounds (safety.spawn.*) — the LLM spawn seams
    # enforce these; 0 = unlimited.
    max_spawn_depth: int
    max_spawn_children: int
    # #2187 for_each S5: pipeline fan-out spawn bounds (safety.spawn.*) — the
    # pipeline executor enforces these (guards b/c); 0 = unlimited.
    max_pipeline_fan_out_depth: int
    max_pipeline_spawns: int

    @classmethod
    def from_config(
        cls, config: Any, project_root: "Any | None" = None,
    ) -> "SessionFactoryConfig":
        """The single mapping point ``ReynConfig`` → the uniform factory args. Add a
        new uniform arg HERE (and as a field above) → all five factory sites get it.

        ``project_root`` (#2575) is required only to LOAD pipelines from disk (the
        scan is project-root-relative). The five frontend factory sites pass it;
        utility/test callers may omit it → an empty PipelineRegistry (no pipelines,
        byte-identical to pre-#2575). It stays optional (not a bundle field) because
        it is a filesystem locus, not a ``ReynConfig``-derived value."""
        from pathlib import Path

        from reyn.data.pipelines.registry import build_pipeline_registry
        from reyn.data.skills.registry import build_skill_registry
        root = Path(project_root) if project_root is not None else None
        pipeline_registry = (
            build_pipeline_registry(config.pipelines, root)
            if root is not None
            else build_pipeline_registry(None, Path.cwd())
        )
        return cls(
            sandbox_config=config.sandbox,
            multimodal_config=config.multimodal,
            action_retrieval_config=config.action_retrieval,
            embedding_config=config.embedding,
            router_config=config.llm.router,
            retry_config=config.llm.retry,
            chat_tool_use_scheme=config.tool_use.chat,
            # #2548 PR-A: build the enabled skill registry once here (filtered to
            # enabled=True) so every factory site threads the same snapshot.
            available_skills=build_skill_registry(config.skills),
            # #2575: built once here (empty when project_root is unknown).
            pipeline_registry=pipeline_registry,
            delegation_capability_default=config.delegation.capability_default,
            max_spawn_depth=config.safety.spawn.max_depth,
            max_spawn_children=config.safety.spawn.max_children,
            max_pipeline_fan_out_depth=config.safety.spawn.max_pipeline_fan_out_depth,
            max_pipeline_spawns=config.safety.spawn.max_pipeline_spawns,
        )
