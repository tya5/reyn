"""reyn.config.loader — config loading + yaml shape-wiring (load_config / _merge / _load_yaml). (#1682 #3 split)."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reyn.config.chat import (  # #1682 #3 cross-section
    _build_chat_config,
    _build_cost_config,  # #1682 #3: cost builder lives in chat
    _build_safety_config,
)
from reyn.config.embedding import (  # #1682 #3 cross-section
    ActionRetrievalConfig,
    _build_action_retrieval_config,
    _build_embedding_config,
    _build_skill_search_config,
)
from reyn.config.execution import (  # #1682 #3 cross-section
    _build_plan_config,
    _build_self_improvement_config,
    _build_skill_resume_config,
    _build_time_travel_config,
    _build_tool_use_config,
)
from reyn.config.infra import (  # #1682 #3 cross-section
    _build_agent_config,
    _build_auth_config,
    _build_cron_config,
    _build_eval_config,
    _build_events_config,
    _build_python_config,
    _build_sandbox_config,
)
from reyn.config.media import (  # #1682 #3 cross-section
    _build_multimodal_config,
    _build_voice_config,
    _build_web_config,
)
from reyn.config.root import ReynConfig, _build_model_class_by_purpose  # #1682 #3 cross-section
from reyn.runtime.budget.budget import CostConfig, CostLimitConfig


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# Cross-tool default resolution order when project_context_path is unset
# (None): AGENTS.md is the convention Claude Code / Codex / opencode / etc.
# all read; REYN.md is the legacy fallback. First existing file wins (mirrors
# opencode's "AGENTS.md beats CLAUDE.md when both exist").
DEFAULT_PROJECT_CONTEXT_FILES: tuple[str, ...] = ("AGENTS.md", "REYN.md")


def load_project_context(config: ReynConfig, project_root: Path) -> str:
    """Read the project context markdown file for the system prompt.

    Resolution:
      - ``project_context_path = None`` (default, unset): auto-resolve the
        cross-tool standard — ``AGENTS.md`` if present, else ``REYN.md``
        (``DEFAULT_PROJECT_CONTEXT_FILES``). First existing file wins.
      - explicit non-empty path: pin exactly that file.
      - explicit ``""``: disabled.

    Returns the chosen file's content stripped, or "" when disabled, none of
    the candidates exist, or the chosen file is unreadable. Empty /
    whitespace-only content also yields "" so callers can short-circuit the
    system-prompt section. The first EXISTING candidate is authoritative even
    if empty (AGENTS.md present-but-empty does not fall through to REYN.md).
    """
    if project_root is None:
        return ""
    rel = config.project_context_path
    if rel is None:
        candidates: tuple[str, ...] = DEFAULT_PROJECT_CONTEXT_FILES
    else:
        rel = rel.strip()
        if not rel:
            return ""
        candidates = (rel,)
    for name in candidates:
        target = project_root / name
        if target.is_file():
            try:
                return target.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
    return ""


def _merge(base: dict, override: dict) -> dict:
    """Merge override into base. models and permissions dicts are shallow-merged; all other keys override."""
    result = dict(base)
    for key, val in override.items():
        if val is None:
            continue
        if key in ("models", "permissions") and isinstance(val, dict):
            result[key] = {**result.get(key, {}), **val}
        elif key == "mcp" and isinstance(val, dict):
            existing = result.get("mcp", {})
            existing_servers = existing.get("servers", {}) if isinstance(existing, dict) else {}
            new_servers = val.get("servers", {}) if isinstance(val, dict) else {}
            # Override-wins for scalar mcp keys (``search_threshold``,
            # ``registries``), server entries union (existing ∪ new). The
            # earlier ``{**existing, "servers": ...}`` form silently dropped
            # the override's non-``servers`` keys, making ``mcp.search_threshold``
            # and ``mcp.registries`` impossible to set from any config layer
            # (they always fell back to the default). Spreading ``val`` after
            # ``existing`` restores last-layer-wins for those scalars while the
            # explicit ``servers`` key keeps the server union intact.
            result["mcp"] = {
                **existing,
                **val,
                "servers": {**existing_servers, **new_servers},
            }
        elif key == "cron" and isinstance(val, dict):
            # FP-0041 #489 PR-B: cron jobs merge by name — dynamic
            # entries (= .reyn/cron.yaml) win on collision with legacy
            # entries (= reyn.yaml cron.jobs[]). Preserves operator
            # hand-edited entries + runtime-registered entries side
            # by side without dropping either.
            existing = result.get("cron", {})
            existing_jobs = existing.get("jobs", []) if isinstance(existing, dict) else []
            new_jobs = val.get("jobs", []) if isinstance(val, dict) else []
            # Build name-keyed dict for union: existing first, then
            # new overrides (= last write wins).
            by_name: dict = {}
            for j in existing_jobs:
                if isinstance(j, dict) and j.get("name"):
                    by_name[j["name"]] = j
            for j in new_jobs:
                if isinstance(j, dict) and j.get("name"):
                    by_name[j["name"]] = j
            result["cron"] = {**existing, "jobs": list(by_name.values())}
        elif key == "chat" and isinstance(val, dict):
            existing = result.get("chat", {})
            if not isinstance(existing, dict):
                existing = {}
            merged_chat = dict(existing)
            for sub_key, sub_val in val.items():
                if sub_key == "memory" and isinstance(sub_val, dict):
                    merged_chat["memory"] = {**existing.get("memory", {}), **sub_val}
                elif sub_key == "compaction" and isinstance(sub_val, dict):
                    existing_comp = existing.get("compaction") or {}
                    existing_caps = existing_comp.get("section_token_caps") or {}
                    new_caps = sub_val.get("section_token_caps") or {}
                    if isinstance(existing_caps, dict) and isinstance(new_caps, dict):
                        sub_val = {
                            **sub_val,
                            "section_token_caps": {**existing_caps, **new_caps},
                        }
                    merged_chat["compaction"] = {**existing_comp, **sub_val}
                else:
                    merged_chat[sub_key] = sub_val
            result["chat"] = merged_chat
        elif key == "safety" and isinstance(val, dict):
            existing = result.get("safety", {})
            if not isinstance(existing, dict):
                existing = {}
            merged_safety = dict(existing)
            for sub_key, sub_val in val.items():
                if sub_key in ("loop", "timeout", "on_limit", "threat_scan") and isinstance(sub_val, dict):
                    merged_safety[sub_key] = {**existing.get(sub_key, {}), **sub_val}
                else:
                    merged_safety[sub_key] = sub_val
            result["safety"] = merged_safety
        else:
            result[key] = val
    return result


def _find_project_root(start: Path) -> Path | None:
    """Walk up from start until finding reyn.yaml, or return None."""
    current = start.resolve()
    while True:
        if (current / "reyn.yaml").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _warn_legacy_dot_reyn_config(path: Path) -> None:
    """Emit a migration warning if a deprecated <project>/.reyn/config.yaml exists.

    ADR-0031 removed this layer from the 3-layer cascade.  The file is
    intentionally NOT loaded — only a warning is emitted so the user can
    migrate the settings to reyn.local.yaml manually.
    """
    if path.exists():
        import sys
        print(
            f"reyn: warning: {path} is deprecated (ADR-0031 — 3-layer config cascade). "
            "Settings in this file are no longer loaded. "
            "Migrate to reyn.local.yaml, then delete this file.",
            file=sys.stderr,
        )


def _parse_mcp_search_threshold(raw_mcp: object) -> int:
    """Extract ``mcp.search_threshold`` from the raw ``mcp:`` section dict.

    Returns the default (30) when the section is absent, the key is missing,
    or the value is invalid. Accepts 0 (= disable the search tool switch).
    """
    _default = 30  # mirrors ReynConfig.mcp_search_threshold default
    if not isinstance(raw_mcp, dict):
        return _default
    threshold_raw = raw_mcp.get("search_threshold", _default)
    try:
        threshold = int(threshold_raw)
        if threshold < 0:
            threshold = 0
        return threshold
    except (TypeError, ValueError):
        return _default


def _reconcile_embedding_class(cfg: "ReynConfig") -> None:
    """#1454 (c)+(d): a class-typed field is closed-world.

    ``action_retrieval.embedding_class`` names an entry in
    ``embedding.classes``. If it names a class with no such entry — the
    builtin ``local-mini`` default when the user REPLACED ``embedding.classes``
    (config.py: user classes override the builtin registry), or a typo — the
    alias can never resolve. Degrade semantic ``search_actions`` to off (None)
    with one decision-enabling log, rather than letting the dangling alias
    reach the embedding backend where it surfaces as a misleading "model not
    found" naming the alias (the owner-reported HF-blocked-company failure).

    Same graceful-degrade family as the missing-extras path; an opt-out-able
    auxiliary feature must never crash a zero-config session.
    """
    import logging

    ec = cfg.action_retrieval.embedding_class
    if not ec or ec in cfg.embedding.classes:
        return
    known = ", ".join(sorted(cfg.embedding.classes)) or "(none)"
    if ec == ActionRetrievalConfig().embedding_class:
        detail = (
            f"the default embedding class {ec!r} has no entry in your "
            f"embedding.classes — add it under embedding.classes, or set "
            f"action_retrieval.embedding_class: null to silence this"
        )
    else:
        detail = (
            f"action_retrieval.embedding_class={ec!r} has no entry in "
            f"embedding.classes (typo?) — add the class or set it to null"
        )
    logging.getLogger(__name__).warning(
        "Semantic search_actions disabled: %s. Known classes: %s.",
        detail, known,
    )
    cfg.action_retrieval.embedding_class = None


def load_config(cwd: Path | None = None) -> ReynConfig:
    """Load and merge config from all sources. CLI flags are applied by the caller."""
    cwd = (cwd or Path.cwd()).resolve()

    # ADR-0030: load ~/.reyn/secrets.env into os.environ before YAML is
    # parsed so that ${VAR} references in any config field resolve correctly.
    from reyn.security.secrets.loader import load_secrets_to_environ
    load_secrets_to_environ()

    # `output_language` intentionally omitted from merged defaults so we
    # can distinguish "user did not configure" (= None, chat router will
    # skip the language directive) from "user explicitly set it" (= str,
    # router prompt enforces it strictly). See `ReynConfig.output_language`.
    merged: dict = {"model": "standard",
                    "models": {}, "permissions": {},
                    "mcp": {}}

    # User global
    user_global = _load_yaml(Path.home() / ".reyn" / "config.yaml")
    merged = _merge(merged, user_global)

    # Project + local
    project_root = _find_project_root(cwd)
    if project_root:
        project = _load_yaml(project_root / "reyn.yaml")
        merged = _merge(merged, project)
        project_local = _load_yaml(project_root / "reyn.local.yaml")
        merged = _merge(merged, project_local)

        # Issue #470: dynamic MCP registry separated from static config.
        # ``.reyn/mcp.yaml`` carries op-managed server entries; merged
        # LAST so it overrides any operator-edited ``mcp.servers`` in
        # reyn.yaml / reyn.local.yaml (= newer installs win, but
        # legacy entries continue to load for backward compat).
        # Shape: ``{"mcp": {"servers": {<name>: {<entry>}}}}`` — same
        # as the section in reyn.yaml, so ``_merge`` handles it
        # without special-casing.
        dynamic_mcp = _load_yaml(project_root / ".reyn" / "mcp.yaml")
        merged = _merge(merged, dynamic_mcp)

        # FP-0041 #489 PR-B: dynamic cron registry separated from static
        # config (= same #470 invariant: ``reyn.yaml`` = edit + restart,
        # ``.reyn/`` = runtime mutable). ``.reyn/cron.yaml`` carries
        # cron jobs registered at runtime via the future LLM-callable
        # cron tool (PR-B2 follow-up). Merged LAST so newer dynamic
        # entries win on name collision with operator-edited
        # ``reyn.yaml`` cron jobs.
        # Shape: ``{"cron": {"jobs": [...]}}`` — same as reyn.yaml
        # cron section. Job-list union via _merge's cron handling.
        dynamic_cron = _load_yaml(project_root / ".reyn" / "cron.yaml")
        merged = _merge(merged, dynamic_cron)

        # ADR-0031: <project>/.reyn/config.yaml is DEPRECATED (removed from
        # the 3-layer cascade).  Emit a one-time warning if the file exists so
        # users know to migrate.  The file is intentionally NOT loaded.
        _warn_legacy_dot_reyn_config(project_root / ".reyn" / "config.yaml")

    # ADR-0030: apply ${VAR} interpolation across all string fields of the
    # merged config dict.  At this point os.environ already contains values
    # loaded from ~/.reyn/secrets.env (see load_secrets_to_environ() above).
    from reyn.security.secrets.interpolation import expand_env
    merged = expand_env(merged)

    # #571 follow-up (post-collapse-arc): propagate ``mcp.registries: [...]``
    # config list into the ``REYN_MCP_REGISTRY_URLS`` env var so the
    # subprocess-side ``reyn.api.safe.mcp.registry`` (= subprocess inherits
    # parent env) and the op-handler-side ``reyn.core.registry.client``
    # (= same process, reads same env var) see the same list. Explicit
    # operator-set env var wins over config (= the standard
    # principle: env var = explicit override, config = declarative
    # baseline). Only the singular ``REYN_MCP_REGISTRY_URL`` legacy
    # form is also respected — when neither plural nor singular env
    # var is set and the config has a list, we export the plural form
    # for the rest of the process to read.
    import os as _os_for_mcp
    if not _os_for_mcp.environ.get("REYN_MCP_REGISTRY_URLS") and not _os_for_mcp.environ.get("REYN_MCP_REGISTRY_URL"):
        raw_registries = merged.get("mcp", {}).get("registries") if isinstance(merged.get("mcp"), dict) else None
        if isinstance(raw_registries, list) and raw_registries:
            urls = [str(u).strip().rstrip("/") for u in raw_registries if isinstance(u, str) and u.strip()]
            if urls:
                _os_for_mcp.environ["REYN_MCP_REGISTRY_URLS"] = ",".join(urls)

    raw_ol = merged.get("output_language")
    output_language: str | None
    if isinstance(raw_ol, str) and raw_ol.strip():
        output_language = raw_ol.strip()
    else:
        # Includes the case where the key is missing entirely AND the
        # case where the user explicitly set output_language to "" or
        # null in yaml (= "I want the OS to not pin a language").
        output_language = None

    safety_raw = merged.get("safety") if isinstance(merged.get("safety"), dict) else {}
    safety = _build_safety_config(safety_raw)
    cost = _build_cost_config(merged.get("cost"))
    _cfg = ReynConfig(
        model=str(merged.get("model", "standard")),
        output_language=output_language,
        models={
            str(k): (v if isinstance(v, dict) else str(v))
            for k, v in (merged.get("models") or {}).items()
        },
        model_class_by_purpose=_build_model_class_by_purpose(
            merged.get("model_class_by_purpose"),
        ),
        tool_calls_op_loop_skills=[
            str(s) for s in (merged.get("tool_calls_op_loop_skills") or [])
        ],
        api_base=str(merged.get("api_base") or ""),
        # prompt_cache_enabled / project_context_path were declared as
        # ReynConfig fields + consumed (llm.py / session.py / agent.py /
        # _read_project_context) but never read here, so operator config was
        # silently ignored (always the dataclass default = a no-op set). Wire
        # them through merged so the operator-set value actually takes effect.
        prompt_cache_enabled=bool(merged.get("prompt_cache_enabled", True)),
        # Absent → None (auto-resolve AGENTS.md → REYN.md in
        # load_project_context). Present → pin that path ("" disables).
        project_context_path=(
            str(merged["project_context_path"])
            if "project_context_path" in merged
            else None
        ),
        permissions=dict(merged.get("permissions") or {}),
        mcp=dict(merged.get("mcp") or {}),
        mcp_search_threshold=_parse_mcp_search_threshold(merged.get("mcp")),
        python=_build_python_config(merged.get("python")),
        agent=_build_agent_config(merged.get("agent")),
        auth=_build_auth_config(merged.get("auth")),
        chat=_build_chat_config(merged.get("chat")),
        events=_build_events_config(merged.get("events")),
        cost=cost,
        skill_resume=_build_skill_resume_config(merged.get("skill_resume")),
        time_travel=_build_time_travel_config(merged.get("time_travel")),
        tool_use=_build_tool_use_config(merged.get("tool_use")),
        plan_resume_raw=(
            merged.get("plan_resume")
            if isinstance(merged.get("plan_resume"), dict) else None
        ),
        voice=_build_voice_config(merged.get("voice")),
        embedding=_build_embedding_config(merged.get("embedding")),
        safety=safety,
        web=_build_web_config(merged.get("web")),
        multimodal=_build_multimodal_config(merged.get("multimodal")),
        skill_search=_build_skill_search_config(merged.get("skill_search")),
        plan=_build_plan_config(merged.get("plan")),
        eval=_build_eval_config(merged.get("eval")),
        sandbox=_build_sandbox_config(merged.get("sandbox")),
        self_improvement=_build_self_improvement_config(merged.get("self_improvement")),
        action_retrieval=_build_action_retrieval_config(merged.get("action_retrieval")),
        cron=_build_cron_config(merged.get("cron")),
        external_transports=_build_external_transports_config(
            merged.get("external_transports"),
        ),
    )
    _reconcile_embedding_class(_cfg)
    return _cfg


def _build_external_transports_config(raw: object):
    """Parse the ``external_transports:`` section (FP-0041 #489 PR-D2).

    Defers to ``reyn.runtime.external_routing.parse_external_transports``
    which handles defensive parsing (= malformed entries silently
    skipped). Lazy import to avoid the same circular dependency
    addressed by ``_empty_external_transports``.
    """
    from reyn.runtime.external_routing import parse_external_transports
    return parse_external_transports(raw)




