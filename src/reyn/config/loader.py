"""reyn.config.loader — config loading + yaml shape-wiring (load_config / _merge / _load_yaml). (#1682 #3 split)."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reyn.config.chat import (  # #1682 #3 cross-section
    _build_chat_config,
    _build_cost_config,  # #1682 #3: cost builder lives in chat
    _build_cost_warn_config,
    _build_offload_config,
    _build_render_template_config,
    _build_safety_config,
)
from reyn.config.embedding import (  # #1682 #3 cross-section
    ActionRetrievalConfig,
    _build_action_retrieval_config,
    _build_embedding_config,
)
from reyn.config.execution import (  # #1682 #3 cross-section
    _build_tool_use_config,
)
from reyn.config.infra import (  # #1682 #3 cross-section
    _build_agent_config,
    _build_auth_config,
    _build_cron_config,
    _build_delegation_config,
    _build_events_config,
    _build_fs_watch_config,
    _build_llm_config,
    _build_python_config,
    _build_sandbox_config,
)
from reyn.config.media import (  # #1682 #3 cross-section
    _build_multimodal_config,
    _build_voice_config,
    _build_web_config,
)
from reyn.config.observability import (
    _build_observability_config,
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


def _as_config_dict(val: object, key: str) -> dict:
    """Coerce a top-level config value to a dict, defaulting on a malformed type.

    A ``models:`` / ``permissions:`` written as a scalar or list in reyn.yaml
    (a user typo) would otherwise crash the loader with an uncaught
    ``AttributeError`` (``.items()`` on a str) / ``ValueError`` (``dict()`` on a
    non-pair list). Default to ``{}`` instead, with a decision-enabling warning
    so the operator learns their config block was ignored rather than silently
    eaten — matches the lenient-default pattern the section builders use.
    """
    if val is None:
        return {}
    if not isinstance(val, dict):
        import logging
        logging.getLogger(__name__).warning(
            "config key %r must be a mapping; got %s — ignoring it.",
            key, type(val).__name__,
        )
        return {}
    return val


def _merge(base: dict, override: dict, *, tier_label: str | None = None) -> dict:
    """Merge override into base. models and permissions dicts are shallow-merged; all other keys override.

    ``tier_label`` (#3100 Axis 4) is an OPTIONAL provenance tag identifying
    which config layer *override* came from (e.g. ``"user_global"`` /
    ``"project"`` / ``"dynamic"``). It is consulted ONLY by the ``skills``
    branch below, to build a same-name-across-tiers collision map for the
    operator-explicit ``:skill`` invocation namespace (#3100 Axis 4: LOUD
    collision — never a silent shadow). Every other caller of ``_merge``
    omits it (default ``None``), which is a no-op — byte-identical to the
    pre-#3100 merge for every non-skills key and for a skills merge with no
    label supplied.
    """
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
        elif key == "llm" and isinstance(val, dict):
            existing = result.get("llm", {})
            if not isinstance(existing, dict):
                existing = {}
            merged_llm = dict(existing)
            for sub_key, sub_val in val.items():
                if sub_key == "router" and isinstance(sub_val, dict):
                    merged_llm["router"] = {**existing.get("router", {}), **sub_val}
                else:
                    merged_llm[sub_key] = sub_val
            result["llm"] = merged_llm
        elif key == "skills" and isinstance(val, dict):
            # #2548 PR-A: skill registry entries union across config tiers —
            # mirrors the mcp.servers merge pattern exactly. Scalar keys
            # last-layer-wins; ``entries`` dict is a union with later tier
            # winning on name collision. Lets ~/.reyn/config.yaml declare
            # global skills while reyn.yaml / .reyn/config/skills.yaml add
            # project-local ones.
            existing = result.get("skills", {})
            existing_entries = existing.get("entries", {}) if isinstance(existing, dict) else {}
            new_entries = val.get("entries", {}) if isinstance(val, dict) else {}
            # #3100 Axis 4: track WHICH tier last declared each skill name, and
            # record a collision the moment a second, DIFFERENTLY-labeled tier
            # declares the same name. This is the only point in the config
            # pipeline that still sees every tier one at a time (load_config
            # calls _merge sequentially, tier by tier) — once entries are
            # unioned below, the losing tier's declaration is gone for good.
            # ``_provenance``/``_collisions`` are internal bookkeeping keys
            # that ride along inside ``skills`` (harmless to every other
            # consumer, which only reads ``entries``) until the operator
            # `:skill` invocation path (reyn.interfaces.skill_invoke) reads
            # ``_collisions`` to fire a LOUD audit-event + warning instead of
            # silently resolving to the last-tier-wins entry.
            existing_provenance = (
                existing.get("_provenance", {}) if isinstance(existing, dict) else {}
            )
            collisions = {
                k: list(v)
                for k, v in (existing.get("_collisions", {}) if isinstance(existing, dict) else {}).items()
            }
            new_provenance = dict(existing_provenance)
            if tier_label is not None:
                for name in new_entries:
                    prior_tier = existing_provenance.get(name)
                    if prior_tier is not None and prior_tier != tier_label:
                        tiers = collisions.setdefault(name, [prior_tier])
                        if prior_tier not in tiers:
                            tiers.append(prior_tier)
                        if tier_label not in tiers:
                            tiers.append(tier_label)
                    new_provenance[name] = tier_label
            result["skills"] = {
                **existing,
                **val,
                "entries": {**existing_entries, **new_entries},
                "_provenance": new_provenance,
                "_collisions": collisions,
            }
        elif key == "pipelines" and isinstance(val, dict):
            # Pipeline registry entries union across config tiers — mirrors the
            # ``skills`` branch above exactly (same #470-style invariant:
            # ``entries`` is a per-name union with later tier winning on
            # collision, not last-tier-wins-wholesale). Lets ~/.reyn/config.yaml
            # declare global pipelines while reyn.yaml / .reyn/config/pipelines.yaml
            # add project-local ones.
            existing = result.get("pipelines", {})
            existing_entries = existing.get("entries", {}) if isinstance(existing, dict) else {}
            new_entries = val.get("entries", {}) if isinstance(val, dict) else {}
            result["pipelines"] = {
                **existing,
                **val,
                "entries": {**existing_entries, **new_entries},
            }
        elif key == "presentations" and isinstance(val, dict):
            # FP-0054 PR-C: named-presentation-template registry entries union across
            # config tiers — mirrors the ``skills`` / ``pipelines`` branches exactly
            # (``entries`` is a per-name union with later tier winning on collision,
            # not last-tier-wins-wholesale). Lets ~/.reyn/config.yaml declare global
            # templates while reyn.yaml / .reyn/config/presentations.yaml add
            # project-local ones.
            existing = result.get("presentations", {})
            existing_entries = existing.get("entries", {}) if isinstance(existing, dict) else {}
            new_entries = val.get("entries", {}) if isinstance(val, dict) else {}
            result["presentations"] = {
                **existing,
                **val,
                "entries": {**existing_entries, **new_entries},
            }
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
    ``embedding.classes``. If it names a class with no such entry — e.g. the
    user set ``embedding_class`` to a built-in class name (``light`` /
    ``standard`` / ``strong``) and then REPLACED ``embedding.classes``
    (config.py: user classes override the builtin registry) without keeping
    that name, or a typo — the alias can never resolve. Degrade semantic
    ``search_actions`` to off (None)
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


def _validate_retrieval_scheme_embedding(cfg: "ReynConfig") -> None:
    """#2895 fix (a): fail loud at config load when ``tool_use.chat:
    retrieval`` is selected with no working embedding configured.

    ``RetrievalScheme`` (``reyn.tools.schemes.retrieval``) presents a
    ``search_actions`` tool the LLM is meant to call before anything else.
    Without an embedding, ``SchemeOps.search_actions`` always returns ``[]``
    (index/provider unavailable — degrades silently by design), and
    retrieval's own terminal rule (empty match minus already-seen ⇒
    terminal) drops the search tool on the very first call — stranding the
    LLM on ``base_tools`` only for the rest of the session, with no catalog
    action ever reachable. The graceful schemes (``enumerate-all`` /
    ``universal-category``) never hit this because their only catalog entry
    point isn't gated behind search, so they degrade via
    ``is_search_available`` (hide the tool + surface a hint) instead of
    going silently dead.

    Reuses the SAME primary gate ``is_search_available`` checks
    (``action_retrieval.embedding_class`` truthy) and the SAME enable-hint
    text those schemes surface via ``list_actions``
    (``universal_catalog._HIDDEN_STATE_HINT``) — one consistent operator
    message regardless of which layer catches the misconfiguration. Runs
    AFTER ``_reconcile_embedding_class`` so a dangling ``embedding_class``
    (typo, no entry in ``embedding.classes``) is already degraded to None
    here too, not just the explicit-null case.

    This is the config-time half of the #2895 fix; ``RetrievalScheme.
    build_presentation`` carries the runtime-auto-fallback half (defense in
    depth for the case this validation is bypassed, e.g. embedding extras
    silently missing at Session-build time — an env fact this config-load
    check cannot see).
    """
    if cfg.tool_use.chat != "retrieval":
        return
    if cfg.action_retrieval.embedding_class:
        return
    from reyn.tools.universal_catalog import _HIDDEN_STATE_HINT

    raise ValueError(
        "tool_use.chat: retrieval requires a working embedding "
        "(action_retrieval.embedding_class is unset/disabled) — without one, "
        "the search_actions tool always returns no results, and retrieval's "
        "terminal-on-empty-match rule drops it on the very first search, "
        "stranding the LLM on base tools only with no catalog action ever "
        "reachable. " + _HIDDEN_STATE_HINT
    )


def _validate_skill_visibility(cfg: "ReynConfig") -> None:
    """Enforce the #2971 clean break: ``skills.entries.<name>.auto_invoke`` is
    removed, and ``visibility`` accepts only its three declared values.

    Raises ``ValueError`` at LOAD (never silently migrating), mirroring
    ``_validate_retrieval_scheme_embedding`` above — the enforce-at-load
    precedent. Two reasons this is a hard error rather than a deprecation
    alias:

    1. ``auto_invoke`` is a MISNOMER, not merely an old name. No mechanism has
       ever auto-invoked a skill (the flag's sole consumer was the L1 menu
       filter), so keeping it as an alias would preserve a name that lies
       about what it does — and the new axis has three states, which no
       boolean can spell.
    2. The rewrite is mechanical and information-preserving, so this error can
       print the operator's EXACT replacement line rather than a direction to
       go read a doc. That is what makes a clean break decision-enabling
       instead of merely obstructive.

    The mapping deliberately preserves each entry's TODAY behavior, not the
    behavior its old doc line promised: ``auto_invoke: false`` documented only
    "excluded from the system-prompt menu", but what it actually DELIVERED was
    total invisibility to the model — because the menu was the only surface
    naming a skill. #2971 adds ``skill_list`` as a second surface, so the two
    readings now diverge; ``false`` maps to ``hidden`` (today's behavior),
    never to ``on_demand`` (the doc's wording). An operator who wants the new
    middle state opts into it explicitly.
    """
    from reyn.data.skills.registry import VISIBILITIES

    entries = cfg.skills.get("entries") if isinstance(cfg.skills, dict) else None
    if not isinstance(entries, dict):
        return

    for name, raw in entries.items():
        if not isinstance(raw, dict):
            continue
        if "auto_invoke" in raw:
            declared = bool(raw.get("auto_invoke"))
            replacement = "menu" if declared else "hidden"
            raise ValueError(
                f"skills.entries.{name}: 'auto_invoke' was removed (#2971) — it never "
                f"controlled auto-invocation (nothing auto-invokes a skill; the flag "
                f"only chose whether the skill was rendered into the system-prompt "
                f"menu), and the replacement axis has three states, not two. Replace "
                f"'auto_invoke: {str(declared).lower()}' with 'visibility: "
                f"{replacement}' to keep this skill behaving exactly as it does "
                f"today. The full axis: 'menu' = rendered into the system-prompt "
                f"menu; 'on_demand' = not in the menu, but discoverable via the "
                f"skill_list tool (new in #2971 — costs no tokens until the model "
                f"asks); 'hidden' = on no model-facing surface at all. See "
                f"docs/concepts/tools-integrations/skills.md."
            )
        if "visibility" in raw and str(raw.get("visibility")) not in VISIBILITIES:
            raise ValueError(
                f"skills.entries.{name}: visibility {str(raw.get('visibility'))!r} is "
                f"not a valid value — expected one of {list(VISIBILITIES)}. 'menu' = "
                f"rendered into the system-prompt menu; 'on_demand' = not in the menu, "
                f"but discoverable via the skill_list tool; 'hidden' = on no "
                f"model-facing surface at all. To turn the skill off entirely use "
                f"'enabled: false', which drops the entry regardless of visibility. "
                f"See docs/concepts/tools-integrations/skills.md."
            )


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

    # proposal 0060 Phase 1 F3a: the builtin tier — code-shipped
    # skills/pipelines/presentations (Addendum A1/A3), merged FIRST so every
    # operator config file below wins on same-name collision (mirrors
    # ``reyn.hooks.schema_registry.BUILTIN_HOOK_SCHEMAS``'s "code ships the
    # floor, config overrides it" shape). ``build_builtin_config`` stamps
    # ``provenance="builtin"`` at THIS loader path (A9) — never via an
    # install op — and ships EMPTY in F3a (mechanism only; F3b populates the
    # exemplar content), so this merge is presently a no-op.
    from reyn.builtin.registry import build_builtin_config
    merged = _merge(merged, build_builtin_config(), tier_label="builtin")

    # User global
    user_global = _load_yaml(Path.home() / ".reyn" / "config.yaml")
    merged = _merge(merged, user_global, tier_label="user_global")

    # Project + local
    project_root = _find_project_root(cwd)
    if project_root:
        project = _load_yaml(project_root / "reyn.yaml")
        merged = _merge(merged, project, tier_label="project")
        project_local = _load_yaml(project_root / "reyn.local.yaml")
        merged = _merge(merged, project_local, tier_label="project_local")

        # Issue #470: dynamic MCP registry separated from static config.
        # ``.reyn/mcp.yaml`` carries op-managed server entries; merged
        # LAST so it overrides any operator-edited ``mcp.servers`` in
        # reyn.yaml / reyn.local.yaml (= newer installs win, but
        # legacy entries continue to load for backward compat).
        # Shape: ``{"mcp": {"servers": {<name>: {<entry>}}}}`` — same
        # as the section in reyn.yaml, so ``_merge`` handles it
        # without special-casing.
        dynamic_mcp = _load_yaml(project_root / ".reyn" / "config" / "mcp.yaml")
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
        dynamic_cron = _load_yaml(project_root / ".reyn" / "config" / "cron.yaml")
        merged = _merge(merged, dynamic_cron)

        # #2548 PR-A: skill registry separated from static config — same
        # #470 invariant as MCP. .reyn/config/skills.yaml carries
        # project-local skill declarations; merged LAST so it wins on
        # name collision with operator-edited reyn.yaml skill entries.
        # Shape: {"skills": {"entries": {<name>: {<entry>}}}} — same
        # as the skills section in reyn.yaml, handled by _merge skills
        # branch above. #2548 PR-B: this file is also in _HOT_RELOAD_FILES
        # (the IN-set) so skill declarations hot-reload at the turn boundary.
        dynamic_skills = _load_yaml(project_root / ".reyn" / "config" / "skills.yaml")
        merged = _merge(merged, dynamic_skills, tier_label="dynamic")

        # Pipeline registry separated from static config — same #470 invariant
        # as skills/MCP. .reyn/config/pipelines.yaml carries project-local
        # pipeline declarations (written by the pipeline_management__install_*
        # tools); merged LAST so it wins on name collision with operator-edited
        # reyn.yaml pipeline entries. Shape: {"pipelines": {"entries": {<name>:
        # {<entry>}}}} — same as the pipelines section in reyn.yaml, handled by
        # the _merge pipelines branch above. Also in _HOT_RELOAD_FILES (the
        # IN-set) so pipeline declarations hot-reload at the turn boundary.
        dynamic_pipelines = _load_yaml(project_root / ".reyn" / "config" / "pipelines.yaml")
        merged = _merge(merged, dynamic_pipelines)

        # FP-0054 PR-C: named-presentation-template registry separated from static
        # config — same #470 invariant as skills/pipelines/MCP.
        # .reyn/config/presentations.yaml carries project-local template
        # declarations; merged LAST so it wins on name collision with
        # operator-edited reyn.yaml presentation entries. Shape:
        # {"presentations": {"entries": {<name>: {<entry>}}}} — same as the
        # presentations section in reyn.yaml, handled by the _merge presentations
        # branch above. Also in _HOT_RELOAD_FILES (the IN-set) so template
        # declarations hot-reload at the turn boundary.
        dynamic_presentations = _load_yaml(project_root / ".reyn" / "config" / "presentations.yaml")
        merged = _merge(merged, dynamic_presentations)

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

    # #1956: propagate ``web.fetch.allow_private_ips`` into the
    # ``REYN_FETCH_ALLOW_PRIVATE_IPS`` env var so the config-less SSRF-guard
    # surfaces read the operator opt-in: the safe.http subprocess (inherits
    # parent env) and the registry main-process modules (reyn.mcp.registry /
    # reyn.core.registry.client, same process). Mirrors the REYN_MCP_REGISTRY_URLS
    # export above. Explicit operator-set env var wins; absent → unset → the
    # guard's fail-secure deny-private default. Only the truthy case is exported
    # (deny is the default, so a False/absent value leaves the var unset).
    if not _os_for_mcp.environ.get("REYN_FETCH_ALLOW_PRIVATE_IPS"):
        _web_cfg = merged.get("web")
        _fetch_cfg = _web_cfg.get("fetch") if isinstance(_web_cfg, dict) else None
        _ap = _fetch_cfg.get("allow_private_ips") if isinstance(_fetch_cfg, dict) else None
        if _ap is True or (isinstance(_ap, str) and _ap.strip().lower() in ("1", "true", "yes", "on")):
            _os_for_mcp.environ["REYN_FETCH_ALLOW_PRIVATE_IPS"] = "1"

    # #2682: propagate ``api_base`` into the ``LITELLM_API_BASE`` env var — the
    # single switch litellm reads (``reyn.llm.llm.proxy_kwargs`` / the embedding
    # ``_proxy_kwargs`` mirror) to route a request to the LiteLLM proxy instead
    # of the real upstream endpoint. ``load_config()`` is the one universal
    # chokepoint EVERY LLM entry point passes before its first LLM call
    # (``reyn pipe run`` / dogfood / embeddings call it directly; chat/run/mcp
    # reach it via ``InvocationContext.from_args``; web via ``_get_registry``),
    # so folding the export here closes the whole class at once — including the
    # embeddings path the per-entry inline copies never covered. Mirrors the
    # REYN_* exports above: explicit operator-set env var wins (idempotent
    # ``setdefault``); an absent/empty ``api_base`` is a no-op. The pre-existing
    # inline copies (``invocation_context.py`` / ``web/deps.py``) are now
    # redundant but harmless (same ``setdefault`` value); their removal + a
    # single-writer AST/CI guard is #2683.
    _api_base = str(merged.get("api_base") or "")
    if _api_base:
        _os_for_mcp.environ.setdefault("LITELLM_API_BASE", _api_base)

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
    cost_warn = _build_cost_warn_config(merged.get("cost_warn"))
    offload = _build_offload_config(merged.get("offload"))
    render_template = _build_render_template_config(merged.get("render_template"))
    _cfg = ReynConfig(
        model=str(merged.get("model", "standard")),
        output_language=output_language,
        models={
            str(k): (v if isinstance(v, dict) else str(v))
            for k, v in _as_config_dict(merged.get("models"), "models").items()
        },
        model_class_by_purpose=_build_model_class_by_purpose(
            merged.get("model_class_by_purpose"),
        ),
        llm=_build_llm_config(merged.get("llm")),
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
        permissions=_as_config_dict(merged.get("permissions"), "permissions"),
        mcp=_as_config_dict(merged.get("mcp"), "mcp"),
        mcp_search_threshold=_parse_mcp_search_threshold(merged.get("mcp")),
        python=_build_python_config(merged.get("python")),
        agent=_build_agent_config(merged.get("agent")),
        delegation=_build_delegation_config(merged.get("delegation")),
        auth=_build_auth_config(merged.get("auth")),
        chat=_build_chat_config(merged.get("chat")),
        events=_build_events_config(merged.get("events")),
        observability=_build_observability_config(merged.get("observability")),
        cost=cost,
        tool_use=_build_tool_use_config(merged.get("tool_use")),
        voice=_build_voice_config(merged.get("voice")),
        embedding=_build_embedding_config(merged.get("embedding")),
        safety=safety,
        cost_warn=cost_warn,
        offload=offload,
        render_template=render_template,
        web=_build_web_config(merged.get("web")),
        multimodal=_build_multimodal_config(merged.get("multimodal")),
        sandbox=_build_sandbox_config(merged.get("sandbox")),
        # #1800 slice 5b: the raw ``hooks:`` block, passed through (parsed by
        # ``load_hooks`` at Session construction). None/absent → empty list.
        hooks=merged.get("hooks") or [],
        # Hook-Event Redesign Phase 4b/5 (#2880/#2881): the raw ``composers:``
        # block, passed through (parsed by ``load_composers`` at Session
        # construction). None/absent → empty list → no Composer starts.
        composers=merged.get("composers") or [],
        action_retrieval=_build_action_retrieval_config(merged.get("action_retrieval")),
        cron=_build_cron_config(merged.get("cron")),
        # #2608 H4: OUT-set only — read from ``merged`` (reyn.yaml/reyn.local.yaml),
        # never from the ``.reyn/*.yaml`` hot-reload IN-set (see
        # ``_HOT_RELOAD_FILES`` below + ``FsWatchConfig``'s docstring for why).
        fs_watch=_build_fs_watch_config(merged.get("fs_watch")),
        external_transports=_build_external_transports_config(
            merged.get("external_transports"),
        ),
        skills=_as_config_dict(merged.get("skills"), "skills"),
        pipelines=_as_config_dict(merged.get("pipelines"), "pipelines"),
        presentations=_as_config_dict(merged.get("presentations"), "presentations"),
    )
    _reconcile_embedding_class(_cfg)
    _validate_retrieval_scheme_embedding(_cfg)
    _validate_skill_visibility(_cfg)
    return _cfg


# ---------------------------------------------------------------------------
# Hot-reload IN-set loader (#2073)
# ---------------------------------------------------------------------------

# The IN-set = the runtime-mutable ``.reyn/*.yaml`` registries (the only files the
# hot-reload mechanism re-reads). The OUT-set (``reyn.yaml`` — security /
# permission / sandbox / budget / the loop valve / state-coupled runtime) is loaded
# ONCE at startup by ``load_config`` and is restart-only; the file-split IS the
# write-gate boundary (owner-confirmed #2073). Keep this list narrow + explicit.
_HOT_RELOAD_FILES: tuple[str, ...] = (
    "config/mcp.yaml", "config/cron.yaml", "config/hooks.yaml",
    "config/skills.yaml",  # #2548 PR-B: skills IN-set hot-reload
    "config/pipelines.yaml",  # pipelines IN-set hot-reload (mirrors skills.yaml)
    "config/presentations.yaml",  # FP-0054 PR-C: presentation-template IN-set hot-reload
)


def load_hot_reload_config(project_root: "Path | None" = None) -> dict:
    """Load ONLY the hot-reloadable IN-set (the runtime-mutable ``.reyn/*.yaml``
    registries) for a config hot-reload (#2073).

    Distinct from :func:`load_config`: this reads **none** of the OUT-set
    (``reyn.yaml`` / ``reyn.local.yaml`` / ``~/.reyn/config.yaml``) — those are
    restart-only. Reading exactly ``.reyn/<f>`` for ``f`` in
    :data:`_HOT_RELOAD_FILES` is the structural safety boundary: a hot-reload (and
    the LLM-op that triggers it) can never touch the OUT-set, because this loader
    never opens those files.

    Returns the merged IN-set dict (``{"mcp": …, "cron": …}``) with ``${VAR}``
    interpolation applied (mirrors ``load_config`` so MCP server secrets resolve).
    An absent ``.reyn/`` dir or missing file yields ``{}`` for that component
    (``_load_yaml`` returns ``{}`` on absence) — a no-op reload, never an error.
    """
    root = (project_root or Path.cwd()).resolve()
    merged: dict = {}
    for fname in _HOT_RELOAD_FILES:
        merged = _merge(merged, _load_yaml(root / ".reyn" / fname))
    from reyn.security.secrets.interpolation import expand_env
    return expand_env(merged)


def load_per_agent_hooks(
    project_root: "Path | None", agent_name: str
) -> list:
    """Load the per-agent runtime hooks layer (#2073 per-agent-hooks add-on) — ONLY
    ``.reyn/agents/<name>/hooks.yaml``.

    Same IN-set grain as the global ``.reyn/hooks.yaml`` (runtime-mutable,
    hot-reloadable) but scoped to one agent — read DIRECTLY here (not via
    :func:`load_hot_reload_config`, which is the top-level ``.reyn/*.yaml`` set),
    mirroring how the per-agent ``profile.yaml`` is read. ``${VAR}`` interpolation is
    applied to match the global layer. Returns the raw ``hooks:`` list (``[]`` when the
    file or key is absent — a no-op layer, never an error).
    """
    root = (project_root or Path.cwd()).resolve()
    raw = _load_yaml(root / ".reyn" / "agents" / agent_name / "hooks.yaml")
    from reyn.security.secrets.interpolation import expand_env
    data = expand_env(raw)
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return hooks if isinstance(hooks, list) else []


def _build_external_transports_config(raw: object):
    """Parse the ``external_transports:`` section (FP-0041 #489 PR-D2).

    Defers to ``reyn.runtime.external_routing.parse_external_transports``
    which handles defensive parsing (= malformed entries silently
    skipped). Lazy import to avoid the same circular dependency
    addressed by ``_empty_external_transports``.
    """
    from reyn.runtime.external_routing import parse_external_transports
    return parse_external_transports(raw)




