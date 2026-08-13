"""#2093: the shared session-factory config bundle — completeness-by-construction.

The five session-factory sites (cli/chat, cli/dogfood, web/deps, cli/mcp,
runtime/registry_bootstrap) each thread a set of UNIFORM, reyn.yaml-config-derived
args identically into ``build_scoped_chat_session`` (8) and ``AgentRegistry`` (3).
Historically a new uniform arg had to be added at all five sites by hand — and was
silently missed at one (``sandbox_config`` at the A2A factory;
``delegation_capability_default`` at ``mcp.py``). #4274 is the same class again, one
level up: ``web_fetch_config`` was declared on ``OpContext`` (#4174 T4) with zero
production construction sites at all — every factory site was silently missing it,
not just one.

``SessionFactoryConfig.from_config`` is the SINGLE point that maps ``ReynConfig`` →
those uniform args. Every site builds the bundle once and passes it to both consumers,
which read their part. A new uniform arg is added in ONE place (the dataclass +
``from_config``) and reaches all five sites — type-enforced, so it cannot be missed.

This is deliberately ONLY the uniform config-derived args. Per-SITE args that
legitimately differ (model / resolver / state_log / the env+sandbox backends /
workspace dirs / contextual_permission / agent_id / allowed_mcp /
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
    # #4274: reyn.yaml web_fetch.* (verify_ssl / allow_private_ips / max_download_bytes,
    # #4174 T4) — declared on OpContext since T4 but never populated at any factory
    # site until this field existed. Same shape as multimodal_config: a plain value,
    # not a per-turn supplier (WebFetchConfig doesn't change mid-session).
    web_fetch_config: Any
    # #4381 PR-5: reyn.yaml read_cap.* (inline_bytes) — the resource-bound
    # per-result inline cap (file.py's read op + load_skill.py). Same
    # shape as web_fetch_config: a plain value, not a per-turn supplier.
    read_cap_config: Any
    # #4387 Phase B ③: reyn.yaml history_resident.* (max_bytes) — the
    # resource-bound cap on Session.history's in-memory footprint. Same
    # shape/role as read_cap_config (#4431's role split): bytes,
    # model-independent, config-driven.
    history_resident_config: Any
    action_retrieval_config: Any
    embedding_config: Any
    router_config: Any
    retry_config: Any
    chat_tool_use_scheme: str
    # #4552 PR-3: moved from action_retrieval.universal_wrappers_enabled —
    # a tool_use.scheme property (only universal-category's own 3 wrapper
    # functions read it), not a retrieval setting. Same resolution-point
    # convention as chat_tool_use_scheme just above.
    chat_universal_wrappers_enabled: bool
    # P5 ADR-0039: the resolved ``observability:`` block (ObservabilityConfig).
    # Opt-in OTLP export surface — reaches every factory site so a session on any
    # frontend attaches the OtelExporter when (and only when) an endpoint is set.
    observability_config: Any
    # #2548 PR-A: enabled skill registry snapshot (list[SkillEntry]), built from
    # config.skills. Uniform config-derived arg → reaches all factory sites.
    available_skills: Any
    # #3100 Axis 4: same-name-across-config-tiers collision map for skills
    # (``{name: [tier, tier, ...]}``), extracted from ``config.skills.
    # _collisions`` (built by ``config.loader._merge``'s skills branch while
    # tiers are still separate). Threaded uniformly so the operator-explicit
    # ``:skill`` invocation path (``reyn.interfaces.skill_invoke``) can fire a
    # LOUD audit-event + warning instead of silently resolving to whichever
    # tier happened to load last.
    skill_collisions: Any
    # #2575: the populated PipelineRegistry, built ONCE per frontend from
    # config.pipelines (disk scan → parse → register). Threaded to every Session
    # (incl. spawns, which reuse this bundle) so the pipelines dir is parsed once
    # per session tree, not re-globbed per session — mirrors the build-once
    # available_skills snapshot. Empty registry when project_root is unknown
    # (direct/test from_config(config)) → byte-identical to pre-#2575.
    pipeline_registry: Any
    # FP-0054 PR-C: the populated PresentationRegistry, built ONCE per frontend from
    # config.presentations (validate each inline blueprint → register by name).
    # Threaded to every Session (incl. spawns) so a named `present` template resolves
    # — mirrors the build-once available_skills / pipeline_registry snapshots. Empty
    # registry when config has no presentations (byte-identical to pre-PR-C).
    presentation_registry: Any
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
        from reyn.data.presentations.registry import build_presentation_registry
        from reyn.data.skills.registry import build_skill_registry
        from reyn.tools.transport import Transport, resolve_scheme_for_transport
        root = Path(project_root) if project_root is not None else None
        pipeline_registry = (
            build_pipeline_registry(config.pipelines, root)
            if root is not None
            else build_pipeline_registry(None, Path.cwd())
        )
        # FP-0054 PR-C: inline blueprints — no filesystem locus, so no project_root
        # dependency (unlike pipelines). Built here so every factory site threads the
        # same validated snapshot.
        presentation_registry = build_presentation_registry(config.presentations)
        return cls(
            sandbox_config=config.sandbox,
            multimodal_config=config.multimodal,
            # #4274: the previously-dead web_fetch.* config, now threaded live.
            web_fetch_config=config.web_fetch,
            # #4381 PR-5: the resource-bound read cap config.
            read_cap_config=config.read_cap,
            # #4387 Phase B ③: the resource-bound history-resident cap config.
            history_resident_config=config.history_resident,
            action_retrieval_config=config.action_retrieval,
            embedding_config=config.embedding,
            router_config=config.llm.router,
            retry_config=config.llm.retry,
            # FP-0066 P4b: resolve the (scheme, transport) 2-axis config
            # through P4a's valid-pair registry to the concrete registered
            # ``ToolUseScheme`` name (e.g. (enumerate-all, content_fence) →
            # the CodeAct cell's resolved name, P4c #3247 — no longer the
            # bare "codeact") — this is the ONE resolution point; every downstream
            # consumer (Session / RouterLoopDriver / RouterLoop) threads the
            # already-resolved concrete name unchanged. Already validated at
            # config-parse time (_build_tool_use_config), so this cannot
            # raise here on a well-formed ReynConfig.
            chat_tool_use_scheme=resolve_scheme_for_transport(
                config.tool_use.scheme, Transport(config.tool_use.transport)
            ),
            # #4552 PR-3: moved from action_retrieval.universal_wrappers_enabled.
            chat_universal_wrappers_enabled=config.tool_use.universal_wrappers_enabled,
            observability_config=config.observability,
            # #2548 PR-A: build the enabled skill registry once here (filtered to
            # enabled=True) so every factory site threads the same snapshot.
            available_skills=build_skill_registry(config.skills),
            # #3100 Axis 4: raw collision map built at merge time; empty dict
            # when config.skills isn't a mapping (lenient, mirrors every other
            # ``.get`` read of this raw dict elsewhere in the loader).
            skill_collisions=(
                config.skills.get("_collisions", {})
                if isinstance(config.skills, dict) else {}
            ),
            # #2575: built once here (empty when project_root is unknown).
            pipeline_registry=pipeline_registry,
            # FP-0054 PR-C: built once here from config.presentations.
            presentation_registry=presentation_registry,
            delegation_capability_default=config.delegation.capability_default,
            max_spawn_depth=config.safety.spawn.max_depth,
            max_spawn_children=config.safety.spawn.max_children,
            max_pipeline_fan_out_depth=config.safety.spawn.max_pipeline_fan_out_depth,
            max_pipeline_spawns=config.safety.spawn.max_pipeline_spawns,
        )
