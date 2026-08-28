"""reyn.config.loader — config loading + yaml shape-wiring (load_config / _merge / _load_yaml). (#1682 #3 split)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from reyn.config.chat import (  # #1682 #3 cross-section
    _build_chat_config,
    _build_cost_config,  # #1682 #3: cost builder lives in chat
    _build_cost_warn_config,
    _build_history_resident_config,
    _build_image_config,
    _build_offload_config,
    _build_read_cap_config,
    _build_render_template_config,
    _build_safety_config,
    _build_tui_config,
)
from reyn.config.embedding import (  # #1682 #3 cross-section
    _build_embedding_config,
)
from reyn.config.execution import (  # #1682 #3 cross-section
    _build_tool_use_config,
)
from reyn.config.infra import (  # #1682 #3 cross-section
    _build_agent_id,
    _build_artifacts_config,
    _build_audit_events_config,
    _build_auth_config,
    _build_cron_config,
    _build_delegation_config,
    _build_fs_watch_config,
    _build_llm_config,
    _build_sandbox_config,
)
from reyn.config.media import (  # #1682 #3 cross-section
    _build_gateway_config,
    _build_multimodal_config,
    _build_voice_config,
    _build_web_fetch_config,
)
from reyn.config.observability import (
    _build_observability_config,
)
from reyn.config.root import ReynConfig  # #1682 #3 cross-section


class HookYamlReadError(ValueError):
    """A hooks.yaml file exists but could not be read as a mapping."""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None) -> None:
        super().__init__(message)
        self.line = line
        self.column = column


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        # A parse failure here is otherwise indistinguishable from "the file
        # doesn't exist" — every section the file would have contributed
        # (models, permissions, ...) silently falls back to built-in/other-tier
        # defaults with no signal the operator's own file was ever read. Log
        # so a malformed reyn.yaml/reyn.local.yaml is at least observable
        # (#3368) — the fallback-to-{} behavior itself is unchanged.
        import logging

        logging.getLogger(__name__).warning(
            "failed to parse %s — ignoring this file's config (falling back "
            "to other config tiers/defaults): %s",
            path, exc,
        )
        return {}


# Cross-tool default resolution order when project_context_path is unset
# (None): AGENTS.md is the convention Claude Code / Codex / opencode / etc.
# all read; REYN.md is the legacy fallback. First existing file wins (mirrors
# opencode's "AGENTS.md beats CLAUDE.md when both exist").
DEFAULT_PROJECT_CONTEXT_FILES: tuple[str, ...] = ("AGENTS.md", "REYN.md")


def resolve_project_context_path(config: ReynConfig, project_root: "Path | None") -> "Path | None":
    """Resolve WHICH file :func:`load_project_context` would read, without
    reading it — the same candidate walk, extracted (#3787) so a caller that
    only needs the path (e.g. the turn-boundary edit-detection watcher) isn't
    forced to also pay for + discard a full read.

    Same resolution as :func:`load_project_context`'s docstring: ``None`` →
    auto-resolve (``AGENTS.md`` else ``REYN.md``, first EXISTING wins);
    explicit non-empty → pin that file; explicit ``""`` → disabled (``None``).
    Returns ``None`` when disabled or no candidate exists.
    """
    if project_root is None:
        return None
    rel = config.project_context_path
    if rel is None:
        candidates: tuple[str, ...] = DEFAULT_PROJECT_CONTEXT_FILES
    else:
        rel = rel.strip()
        if not rel:
            return None
        candidates = (rel,)
    for name in candidates:
        target = project_root / name
        if target.is_file():
            return target
    return None


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
    target = resolve_project_context_path(config, project_root)
    if target is None:
        return ""
    try:
        return target.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_policy_tier_config(cwd: Path | None = None) -> dict:
    """Rebuild JUST the operator-editable policy tier (builtin +
    user_global + project + project_local, merged) — the same dict
    :func:`load_config` validates via :func:`_warn_unknown_config_keys`
    right after ``project_local``, before the 5 dynamic registry files
    merge in.

    #4174 T0b: the one call `reyn config validate` (CLI) uses so it checks
    EXACTLY what startup checks, not a second hand-reconstructed merge —
    architect's explicit requirement ("same implementation as startup,
    called from both places").
    """
    cwd = (cwd or Path.cwd()).resolve()
    # #4174 T3: model/models seed moved under llm — _build_llm_config's own
    # defaults (model="standard", models={}) apply when llm/llm.model/
    # llm.models are absent, so no seed is needed for them here anymore.
    merged: dict = {"permissions": {}, "mcp": {}}

    from reyn.builtin.registry import build_builtin_config
    merged = _merge(merged, build_builtin_config(), tier_label="builtin")

    user_global = _load_yaml(Path.home() / ".reyn" / "config.yaml")
    merged = _merge(merged, user_global, tier_label="user_global")

    project_root = _find_project_root(cwd)
    if project_root:
        project = _load_yaml(project_root / "reyn.yaml")
        merged = _merge(merged, project, tier_label="project")
        project_local = _load_yaml(project_root / "reyn.local.yaml")
        merged = _merge(merged, project_local, tier_label="project_local")

    return merged


def _warn_unknown_config_keys(
    policy_tier_merged: dict,
) -> "dict[str, Any]":
    """#4174 T0: log ONE warning naming every unknown/renamed config key
    found in the policy-tier config (``user_global`` + ``project`` +
    ``project_local`` merged — the operator-editable files; the 5
    hot-reload registry files are NOT included, they're checked separately
    at their own load points, see ``runtime.hot_reload.validate_in_set``).

    Owner ruling (accumulated across #4174's spec revisions): warn, never
    hard-fail, anywhere — including ``sandbox.policy`` (no special case).
    Every unknown key is collected and reported in ONE pass (never an
    early-return "fix one, restart, hit the next" loop). Each line states
    the consequence as "not applied" (never just "unknown" — the operator
    needs to know their config had no effect, not just that a word was
    unrecognized), names the exact new destination when the key was
    renamed, and points at ``reyn config migrate`` as the fix command.
    A ``sandbox.*``-rooted key gets an EXTRA line naming the effective
    resolved sandbox policy — dropping a policy key makes the config
    LOOSER, not silently inert like an ordinary dropped key, so an
    operator relying on it must see what's actually in force.

    Returns the full ``{dotted_key: hint_or_None}`` dict — #4357: this
    used to return only the bare COUNT (#4194: the log-only warning is
    invisible in the interactive CUI — ``_setup_interactive_logging``
    redirects all logs to a file, per architect's live measurement — so
    the count was returned for :func:`load_config` to attach to the
    ``ReynConfig`` it builds). The count alone gave the CUI's bottom
    chrome something to show, but not WHICH keys — an operator reading
    "3 config keys not applied" cannot act on it without separately
    running ``reyn config validate``, and #4357 measured that in practice
    nobody did (5 real instances of a moved key going unfixed for months,
    including this repo's own ``reyn.yaml``). ``unknown_config_keys()``
    already carries per-key ``RenamedKeyHint``/``RemovedKeyHint`` detail
    (#4375/#4402) — the data was never the gap, only how far it traveled.
    Callers that only need the count use ``len(...)`` on the return value.
    """
    from reyn.config import config_schema

    unknown = config_schema.unknown_config_keys(policy_tier_merged)
    if not unknown:
        return {}

    import logging
    log = logging.getLogger(__name__)

    lines = [
        f"config key {key!r} is not recognized — it was NOT APPLIED"
        + (f"; {hint.note}" if hint else ".")
        for key, hint in sorted(unknown.items())
    ]
    lines.append("Run `reyn config migrate` to fix renamed keys automatically.")

    if any(key.startswith("sandbox.") for key in unknown):
        from reyn.security.sandbox.policy import resolve_sandbox_policy

        sandbox_raw = policy_tier_merged.get("sandbox")
        policy_raw = sandbox_raw.get("policy") if isinstance(sandbox_raw, dict) else None
        mode = sandbox_raw.get("mode", "compat") if isinstance(sandbox_raw, dict) else "compat"
        resolved = resolve_sandbox_policy(
            policy_raw if isinstance(policy_raw, dict) else None,
            write_paths=[], mode=str(mode),
        )
        lines.append(
            f"Effective sandbox policy in force right now (unknown keys "
            f"above are excluded from it): {resolved!r}"
        )

    log.warning("Unrecognized config key(s) found:\n" + "\n".join(f"  - {line}" for line in lines))
    return unknown


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
        # #4174 T3: top-level `models:` is a renamed (now-unknown) key — this
        # branch still shallow-merges it if present (harmless: the merged
        # raw dict is never read into ReynConfig anymore, only surfaced by
        # the unknown-key walk), so a legacy config's cross-tier behavior
        # doesn't change shape while the operator migrates. The LIVE
        # location is `llm.models`, handled in the `key == "llm"` branch
        # below.
        if key in ("models", "permissions") and isinstance(val, dict):
            result[key] = {**result.get(key, {}), **val}
        elif key == "mcp" and isinstance(val, dict):
            existing = result.get("mcp", {})
            existing_servers = existing.get("servers", {}) if isinstance(existing, dict) else {}
            new_servers = val.get("servers", {}) if isinstance(val, dict) else {}
            # Override-wins for scalar mcp keys (e.g. ``registries``), server
            # entries union (existing ∪ new). The earlier ``{**existing,
            # "servers": ...}`` form silently dropped the override's
            # non-``servers`` keys, making ``mcp.registries`` impossible to
            # set from any config layer (it always fell back to the
            # default). Spreading ``val`` after ``existing`` restores
            # last-layer-wins for those scalars while the explicit
            # ``servers`` key keeps the server union intact. (``search_threshold``
            # was one such scalar historically; #3218/FP-0066 §7 P1a fold-removed
            # it as a dead config field — ``mcp_search_threshold`` is now purely a
            # ``build_tools()`` function parameter, not read from this ``mcp``
            # config dict at all — see ``router_tools.py``.)
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
                # #4174 T3: llm.models shallow-merges by key, same as the
                # legacy top-level `models:` special-case above — a project
                # tier adding one model class must not drop a user-global
                # class it didn't mention.
                elif sub_key == "models" and isinstance(sub_val, dict):
                    merged_llm["models"] = {**existing.get("models", {}), **sub_val}
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


# maxsize=None (unbounded): keys are distinct resolved cwd-like starting
# paths a single process is asked about, naturally a handful at most
# (production callers all resolve from `Path.cwd()`) — not a value with
# unbounded cardinality over a process's lifetime.
@lru_cache(maxsize=None)
def _find_project_root_uncached(resolved_start: Path) -> Path | None:
    """The actual filesystem walk — never call directly, see `_find_project_root`.

    ``resolved_start`` must already be resolved (the caller does this once,
    so this cached function's key is the real, canonical path — an
    unresolved and a resolved path pointing at the same directory must hit
    the same cache entry, not two)."""
    current = resolved_start
    while True:
        if (current / "reyn.yaml").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _find_project_root(start: Path) -> Path | None:
    """Walk up from start until finding reyn.yaml, or return None.

    #3681 (#3671 P4 item A-3): single-owner cache keyed on the resolved
    ``start`` path, so a process that calls this N times for the same
    starting directory (`reyn chat` alone did it 3x: interactive-logging
    setup, `load_config()`, `build_environment_backend()`) walks the
    filesystem once, not N times, WITHOUT any caller needing to thread a
    pre-computed value through — the fix an optional `project_root=` kwarg
    (rejected, #3678 review) could not give: a caller that forgets to pass
    it silently reverts to walking again, undetectably.

    A real `reyn` process runs this walk against a project directory whose
    ancestor `reyn.yaml` presence does not change mid-run, so this is safe
    in production. It is NOT safe across a whole pytest session sharing one
    interpreter, where many tests write a fresh `reyn.yaml` under a
    `tmp_path` after this may have already cached a miss for that exact
    path — `tests/conftest.py`'s `_clear_find_project_root_cache` autouse
    fixture clears the cache before every test so each test's own writes
    are observed. Call :func:`_find_project_root_uncached`'s own
    ``cache_clear()`` directly if a test needs to invalidate mid-test
    (querying the same path, writing `reyn.yaml`, then querying again)."""
    return _find_project_root_uncached(start.resolve())


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


def _validate_retrieval_scheme_embedding(cfg: "ReynConfig") -> None:
    """#2895 fix (a): fail loud at config load when ``tool_use.scheme:
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

    Reuses the SAME primary gate ``is_search_available`` checks (FP-0066 §7:
    ``embedding.enabled`` truthy — clean-break replacement for the retired
    ``action_retrieval.embedding_class`` gate) and the SAME enable-hint text
    those schemes surface via ``list_actions``
    (``universal_catalog._HIDDEN_STATE_HINT``) — one consistent operator
    message regardless of which layer catches the misconfiguration. The
    embedding CLASS itself (``embedding.default_class`` / a dangling
    ``embedding.classes`` reference) is validated eagerly by
    ``_build_embedding_config`` at parse time (raises there), so by the time
    this runs ``embedding.enabled`` implies a resolvable class.

    This is the config-time half of the #2895 fix; ``RetrievalScheme.
    build_presentation`` carries the runtime-auto-fallback half (defense in
    depth for the case this validation is bypassed, e.g. embedding extras
    silently missing at Session-build time — an env fact this config-load
    check cannot see).
    """
    if cfg.tool_use.scheme != "retrieval":
        return
    if cfg.embedding.enabled:
        return
    from reyn.tools.universal_catalog import _HIDDEN_STATE_HINT

    raise ValueError(
        "tool_use.scheme: retrieval requires a working embedding "
        "(embedding.enabled is false) — without one, "
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
    #
    # proposal 0060 Phase 1 F3a: the builtin tier (code-shipped
    # skills/pipelines/presentations) is merged FIRST, then user_global,
    # then project + project_local — this is the operator-editable
    # "policy tier" #4174 T0 validates. build_policy_tier_config is the
    # SAME construction `reyn config validate` (CLI) uses, so the two
    # never independently drift apart.
    merged = build_policy_tier_config(cwd)

    # Project + local
    project_root = _find_project_root(cwd)
    if project_root:
        # #4174 T0: unknown-key WARN check on the policy tier ONLY (builtin
        # + user_global + project + project_local), before the 5 dynamic
        # registry files below are merged in — those are checked separately
        # at their own load points (runtime.hot_reload.validate_in_set),
        # not duplicated here.
        unknown_config_keys_found = _warn_unknown_config_keys(merged)

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
        # pipeline declarations (written by the pipeline_install_local /
        # pipeline_install_source tools); merged LAST so it wins on name collision with operator-edited
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
    else:
        # #4174 T0: no project root — only builtin + user_global are in
        # `merged`, still worth checking (a mistyped ~/.reyn/config.yaml
        # key should not go unreported just because there's no project).
        unknown_config_keys_found = _warn_unknown_config_keys(merged)

    # #5166: reyn's OWN token vocabulary (${REYN_PROJECT_DIR} — the only one
    # this project-wide, agent-less load has a real value for;
    # ${REYN_AGENT_NAME} has no meaning here and is deliberately NOT in this
    # map, same as any other token this pass does not recognise) is expanded
    # FIRST, via expand_with_map — never os.environ, the same reasoning
    # load_per_agent_hooks/read_and_expand_hooks_yaml use. ORDER MATTERS:
    # this must run BEFORE expand_env below. expand_with_map's own contract
    # leaves anything absent from its map untouched (an MCP server's
    # ${API_KEY} is not a reyn token, so this pass never touches it) — doing
    # it in the other order would let expand_env's os.environ lookup consume
    # ${REYN_PROJECT_DIR} first (undefined there, silently degrading to ""),
    # exactly the #5140 failure shape one layer up. No fail-close here
    # (unlike the per-agent/per-session hooks.yaml layers): an unresolved
    # ${REYN_AGENT_NAME} in reyn.yaml itself has no agent context to supply
    # it at this project-wide load — refusing the WHOLE config over one
    # per-agent token would be a disproportionate blast radius this
    # project-wide layer was never asked to take on (architect ruling on
    # #5166 scopes fail-close to the 4 enumerated hooks.yaml layers only).
    from reyn.plugins.tokens import expand_with_map
    merged = expand_with_map(merged, {"REYN_PROJECT_DIR": str(project_root or cwd)})

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

    # #1956: propagate ``web_fetch.allow_private_ips`` into the
    # ``REYN_FETCH_ALLOW_PRIVATE_IPS`` env var so the config-less SSRF-guard
    # surfaces read the operator opt-in: the safe.http subprocess (inherits
    # parent env) and the registry main-process modules (reyn.mcp.registry /
    # reyn.core.registry.client, same process). Mirrors the REYN_MCP_REGISTRY_URLS
    # export above. Explicit operator-set env var wins; absent → unset → the
    # guard's fail-secure deny-private default. Only the truthy case is exported
    # (deny is the default, so a False/absent value leaves the var unset).
    #
    # #4174 T4/#4317: was ``merged.get("web").get("fetch")`` (the pre-T4
    # nested key) — T4 split ``web:`` into a top-level ``web_fetch:``
    # scalar-namespace (see :func:`reyn.config.media._build_web_fetch_config`,
    # already reading the correct top-level key below at ``web_fetch=``) and
    # a top-level ``gateway:`` block. This export block was never updated,
    # so ``merged.get("web")`` always resolved to the unknown-key ``None``
    # after T4 landed and the export silently stopped firing — fail-secure
    # (the guard's own deny-private default took over), but "configured yet
    # not applied" is its own bug distinct from a hole.
    if not _os_for_mcp.environ.get("REYN_FETCH_ALLOW_PRIVATE_IPS"):
        _web_fetch_cfg = merged.get("web_fetch")
        _ap = _web_fetch_cfg.get("allow_private_ips") if isinstance(_web_fetch_cfg, dict) else None
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
    # #4174 T3: ``api_base`` moved from top-level to ``llm.api_base``.
    _llm_raw = merged.get("llm")
    _api_base = str((_llm_raw.get("api_base") if isinstance(_llm_raw, dict) else None) or "")
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
    read_cap = _build_read_cap_config(merged.get("read_cap"))
    history_resident = _build_history_resident_config(merged.get("history_resident"))
    image = _build_image_config(merged.get("image"))
    tui = _build_tui_config(merged.get("tui"))
    # #4174 T3: model / models / model_class_by_purpose / api_base /
    # prompt_cache_enabled moved from top-level ReynConfig fields into
    # ``llm:`` — _build_llm_config parses all of it (router/retry AND the
    # T3 fields) from the SAME raw ``llm`` dict, in one place.
    _cfg = ReynConfig(
        output_language=output_language,
        llm=_build_llm_config(merged.get("llm")),
        # Absent → None (auto-resolve AGENTS.md → REYN.md in
        # load_project_context). Present → pin that path ("" disables).
        project_context_path=(
            str(merged["project_context_path"])
            if "project_context_path" in merged
            else None
        ),
        permissions=_as_config_dict(merged.get("permissions"), "permissions"),
        mcp=_as_config_dict(merged.get("mcp"), "mcp"),
        agent_id=_build_agent_id(merged.get("agent_id")),
        delegation=_build_delegation_config(merged.get("delegation")),
        auth=_build_auth_config(merged.get("auth")),
        chat=_build_chat_config(merged.get("chat")),
        audit_events=_build_audit_events_config(merged.get("audit_events")),
        artifacts=_build_artifacts_config(merged.get("artifacts")),
        observability=_build_observability_config(merged.get("observability")),
        cost=cost,
        tool_use=_build_tool_use_config(merged.get("tool_use")),
        voice=_build_voice_config(merged.get("voice")),
        embedding=_build_embedding_config(merged.get("embedding")),
        safety=safety,
        cost_warn=cost_warn,
        offload=offload,
        render_template=render_template,
        read_cap=read_cap,
        history_resident=history_resident,
        image=image,
        tui=tui,
        web_fetch=_build_web_fetch_config(merged.get("web_fetch")),
        gateway=_build_gateway_config(merged.get("gateway")),
        multimodal=_build_multimodal_config(merged.get("multimodal")),
        sandbox=_build_sandbox_config(merged.get("sandbox")),
        # #1800 slice 5b: the raw ``hooks:`` block, passed through (parsed by
        # ``load_hooks`` at Session construction). None/absent → empty list.
        hooks=merged.get("hooks") or [],
        # Hook-Event Redesign Phase 4b/5 (#2880/#2881): the raw ``composers:``
        # block, passed through (parsed by ``load_composers`` at Session
        # construction). None/absent → empty list → no Composer starts.
        composers=merged.get("composers") or [],
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
        # #4194: policy-tier unknown-key count — see the field's own
        # docstring in root.py for why this is `schema_internal` (a
        # runtime-computed fact, not an operator-settable key).
        unknown_config_key_count=len(unknown_config_keys_found),
        # #4357: the full `{dotted_key: hint}` dict the count above is
        # derived from — see the field's own docstring in root.py for why
        # the count alone wasn't enough (an operator can't act on a bare
        # number).
        unknown_config_keys=unknown_config_keys_found,
    )
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


def _load_hooks_yaml(path: Path) -> dict:
    """Read a hooks layer while preserving parse failure for its caller."""
    if not path.exists():
        return {}
    try:
        import yaml
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("failed to parse %s: %s", path, exc)
        mark = getattr(exc, "problem_mark", None)
        raise HookYamlReadError(
            str(exc),
            line=getattr(mark, "line", None),
            column=getattr(mark, "column", None),
        ) from exc
    return data if isinstance(data, dict) else {}


def read_and_expand_hooks_yaml(
    path: Path, *, agent_name: str, project_root: Path,
) -> "dict | None":
    """#5166 (architect ruling, issuecomment-5384196419): the ONE read+expand
    primitive EVERY hooks.yaml-shaped layer goes through — per-agent AND
    per-session, the ``hooks:`` key AND the ``composers:`` key alike (same
    file on disk, same 2 keys; only the caller's own key extraction
    differs — see :meth:`~reyn.runtime.session.Session._hooks_yaml_layers`
    for the enumerable registry of callers).

    **Why one primitive, not "a rule everyone remembers"**: hooks get
    copy-pasted between layers (the #5164 docstring's own "mirrors" —
    architect's evidence this issue cites). A rule duplicated 4 times
    means the NEXT copy only carries whichever half someone remembered —
    exactly what happened here: #5161 fixed the per-agent ``hooks:``
    reader, #5164 caught up the per-agent ``composers:`` reader, and the
    2 per-session readers had NO expansion at all until this function
    unified all 4 onto one implementation.

    Expands reyn's OWN token vocabulary via
    :func:`reyn.plugins.tokens.expand_with_map` with an explicit
    ``{REYN_PROJECT_DIR, REYN_AGENT_NAME}`` map — never ``os.environ``
    (``expand_env``, ADR-0030): that expander is for a SPAWNED CHILD
    process's own config-time env-injection, and ``REYN_AGENT_NAME`` is
    only ever set on a child's env, never on THIS process's own
    ``os.environ`` — so it is always undefined here regardless of layer
    (#5140's original finding). Fails closed via
    :func:`~reyn.plugins.tokens.find_unresolved_reyn_tokens` on any
    REMAINING ``${REYN_*}``/``${CLAUDE_*}`` token (reyn's own bug, not an
    operator's config choice) — returns ``None`` (never ``{}``, so a
    caller can tell "genuinely absent" from "refused" if it ever needs
    to) — while an arbitrary non-reyn ``${FOO}`` is left untouched, for
    whatever downstream (a spawned child process) resolves it later.

    Returns ``None`` when the file is absent or refused; otherwise the
    parsed+expanded dict — the caller reads whichever key
    (``hooks``/``composers``) it owns out of it. A file that EXISTS but
    does not parse raises :class:`HookYamlReadError` instead of joining
    the ``None`` cases (#5100): collapsing it there made "this layer
    declares no hooks" and "this layer could not be read" the same value,
    so no caller could tell them apart or report the difference."""
    raw = _load_hooks_yaml(path)
    if not raw:
        return None
    from reyn.plugins.tokens import expand_with_map, find_unresolved_reyn_tokens
    data = expand_with_map(
        raw, {"REYN_PROJECT_DIR": str(project_root), "REYN_AGENT_NAME": agent_name}
    )
    unresolved = find_unresolved_reyn_tokens(data)
    if unresolved:
        import warnings
        warnings.warn(
            f"{path} left reyn token(s) {sorted(set(unresolved))} "
            "unresolved -- refusing to load this hooks.yaml layer (this "
            "is reyn's own bug, not a config choice to honor).",
            UserWarning,
            stacklevel=2,
        )
        return None
    return data if isinstance(data, dict) else None


def load_per_agent_hooks(
    project_root: "Path | None", agent_name: str
) -> list:
    """Load the per-agent runtime hooks layer (#2073 per-agent-hooks add-on) — ONLY
    ``.reyn/agents/<name>/hooks.yaml``.

    Same IN-set grain as the global ``.reyn/config/hooks.yaml`` (runtime-mutable,
    hot-reloadable) but scoped to one agent — read DIRECTLY here (not via
    :func:`load_hot_reload_config`, which is the top-level ``.reyn/*.yaml`` set),
    mirroring how the per-agent ``profile.yaml`` is read. Returns the raw
    ``hooks:`` list (``[]`` when the file or key is absent, malformed, or
    refused for an unresolved reyn token — a no-op layer, never an error
    the caller has to handle specially).

    #5140/#5166: token expansion is :func:`read_and_expand_hooks_yaml` — see
    that function's own docstring for the full reasoning (why
    ``expand_with_map`` not ``expand_env``, why fail-closed on reyn's own
    tokens only)."""
    root = (project_root or Path.cwd()).resolve()
    path = root / ".reyn" / "agents" / agent_name / "hooks.yaml"
    data = read_and_expand_hooks_yaml(path, agent_name=agent_name, project_root=root)
    hooks = (data or {}).get("hooks")
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




