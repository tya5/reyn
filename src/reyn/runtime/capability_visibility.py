"""CapabilityVisibility — the per-session capability/skill VISIBILITY subsystem
(#2285, extracted from ``Session`` at #3121 step3 / Extract Class).

``Session`` historically owned the ``_visibility_override`` toggle state
directly, plus the six methods that read/write it (the status-bar seam:
show/hide a tool, MCP server, category, or skill for THIS session only,
restrict-only on top of the resolved agent envelope). This module extracts
that cohesive field+method cluster into an INDEPENDENT class that OWNS the
state — ``Session`` holds exactly one reference (``self._capability_visibility``)
and delegates; it does not construct a bundle and unpack it back into its own
fields (the #3082 Fowler anti-pattern this extraction is designed to avoid).

Ownership split:

- **Owned here**: ``_visibility_override`` (the toggle set, tool/mcp/category/skill),
  and the two live-resolved fields it composes with the agent envelope,
  ``contextual_permission`` / ``excluded_categories`` — both are mutated ONLY by
  ``apply_per_session_narrowing`` and ``reapply_visibility_override`` (verified: no
  other ``Session`` code path reassigns them), so full ownership here avoids a
  second, potentially-stale copy on ``Session``.
- **Injected dependency (constructor)**: ``registry`` / ``router_host`` /
  ``agent_name`` — stable for the session's lifetime, read but never mutated
  here. ``available_skills_provider`` and ``session_id_provider`` are zero-arg
  callables reading ``Session._available_skills`` / ``Session._session_id``
  LIVE — both are Session-owned state that CAN be reassigned post-construction
  by the owning ``AgentRegistry`` (skill hot-reload; spawn-time session_id
  re-key, ``registry.py`` ``spawn_session_recorded`` — a snapshot taken once
  at construction would go stale and silently re-derive the envelope against
  the WRONG session id after a re-key), so this class reads through a live
  getter rather than owning a second, staleable copy.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from pathlib import Path


class _EnvelopeSource(Protocol):
    """The one method this class needs from its ``registry`` dep (the
    ``AgentRegistry``): resolve the agent's authorized envelope for a session.
    A Protocol keeps ``CapabilityVisibility`` decoupled from the concrete
    registry type (no import of a Session sibling) while giving Pyright the
    attribute it verifies."""

    def resolved_profile_for(
        self, agent_name: str, *, sid: "str | None",
    ) -> "tuple[object | None, frozenset[str]]": ...


class _RouterHost(Protocol):
    """The seams this class needs from its ``router_host`` dep (the
    ``RouterHostAdapter``): the live MCP-server roster (read) + the filtered
    skill list (write), plus (#3220) the same host accessors ``RouterLoop.
    SchemeOps.present`` / ``base_tools`` call to build the ``tools=`` payload —
    so ``capability_visibility_state`` can derive the "tool" census from the
    SAME ``build_tools()`` substrate the composed per-turn payload uses,
    instead of a raw global-registry census (#3220 ground-truth: the two
    diverge — a ``gates.router != "allow"`` tool, or a name the active scheme's
    wrapper-collapse strips, is registry-visible but never payload-reachable).
    Protocol, same decoupling rationale as ``_EnvelopeSource``.
    ``_available_skills`` is a live-mutated attribute, not a property, so it is
    typed as a plain field here."""

    _available_skills: "list | None"

    def get_mcp_servers(self) -> "list[dict]": ...
    def list_available_agents(self) -> "list[dict]": ...
    def get_file_permissions(self) -> "dict | None": ...
    def get_web_fetch_allowed(self) -> bool: ...
    def get_sandbox_backend(self) -> "str | None": ...


class _SkillEntry(Protocol):
    """The one field this class reads off each available-skill entry
    (``SkillEntry.name``)."""

    name: str


class CapabilityVisibility:
    """Owns the per-session capability/skill visibility override (#2285) —
    the status-bar seam's live state + the methods that read/write it.
    Restrict-only on top of the resolved agent envelope: ``visible ⊆
    authorized`` always holds (security core), never re-granted beyond it."""

    def __init__(
        self,
        *,
        registry: "_EnvelopeSource | None",
        router_host: "_RouterHost",
        session_id_provider: "Callable[[], str | None]",
        agent_name: "str",
        available_skills_provider: "Callable[[], list[_SkillEntry] | None]",
        contextual_permission: "object | None" = None,
        excluded_categories: "frozenset[str] | None" = None,
        chat_tool_use_scheme: "str" = "enumerate-all",
    ) -> None:
        self._registry = registry
        self._router_host = router_host
        self._session_id_provider = session_id_provider
        # Immutable for the session's lifetime (Agent is frozen), same stability class as
        # agent_name — needed by resolved_profile_for(agent_name, sid=...) in the two
        # envelope-resolving methods below.
        self._agent_name = agent_name
        self._available_skills_provider = available_skills_provider
        self._contextual_permission = contextual_permission
        self._excluded_categories = frozenset(excluded_categories or ())
        # #3220: the chat-layer ``ToolUseScheme`` name (``reyn.tools.scheme.get_scheme``
        # registry key — "enumerate-all" / "universal-category" / "codeact" / "retrieval").
        # Immutable for the session's lifetime (Session never reassigns
        # ``self._chat_tool_use_scheme`` post-construction — same stability class as
        # ``agent_name``), so a plain field is correct here, not a live provider.
        self._chat_tool_use_scheme = chat_tool_use_scheme
        # Session-scoped LLM tool-VISIBILITY override, restrict-only on top of the resolved agent envelope (#2285)
        self._visibility_override: "dict[str, set[str]]" = {
            "tool": set(), "mcp": set(), "category": set(), "skill": set(),
        }

    @property
    def contextual_permission(self) -> "object | None":
        """The live ``ContextualPermission`` (#1827 S3) — the per-turn gate
        value ``reapply_visibility_override`` maintains (envelope ∩ session
        override, restrict-only, narrow-only)."""
        return self._contextual_permission

    @property
    def excluded_categories(self) -> "frozenset[str]":
        """The live excluded-category set (envelope ∩ session override)."""
        return self._excluded_categories

    @property
    def visibility_override(self) -> "dict[str, set[str]]":
        """Read-only-by-convention view of the toggle state (tool/mcp/category/skill
        -> hidden names). Callers should mutate only through ``set_capability_visible``."""
        return self._visibility_override

    def apply_per_session_narrowing(
        self, contextual_permission: "object | None", excluded_categories,
    ) -> None:
        """#2126: re-inject the spawner-set per-session capability narrowing AFTER
        spawn-time config resolution.

        The #1827 / #2103-S1a per-session layer only composes when
        ``resolved_profile_for`` is called WITH a ``sid`` — and no construction-time
        factory caller passes one (every frontend resolves ``sid=None``), so the
        narrowing a spawner writes to the session's ``config.yaml`` is otherwise never
        enforced (``contextual_permission`` is set once at construction from the
        ``sid=None`` resolution). The registry calls this right after spawn-recording,
        BEFORE the session's run-loop reads these into the live tool gate, so the first
        turn already gates against the narrowing.

        ``contextual_permission`` is the FULL ``resolved_profile_for(name, sid=sid)``
        composition (topology + delegate floor + per-session ∩), so it is overwritten —
        it can only be MORE restrictive than the ``sid=None`` value it replaces (the
        per-session config is an extra ∩ conjunct, never a re-grant). ``excluded_categories``
        is UNIONED (never overwritten) so it composes with any construction-time view
        narrowing (e.g. the #1667 eval ``reyn_repo`` exclusions, which are not
        capability-profile-derived).
        """
        self._contextual_permission = contextual_permission
        self._excluded_categories = self._excluded_categories | frozenset(
            excluded_categories or ()
        )

    # ── #2285: session-scoped LLM tool-VISIBILITY toggle (the status-bar seam) ──────────────

    def reapply_visibility_override(self) -> None:
        """#2285: recompute the live tool gate from the agent envelope ∩ the session override.

        SECURITY CORE (visible ⊆ authorized): re-resolves the WHOLE agent envelope from base
        (topology bindings ∩ the #2081 delegate floor ∩ the persisted per-session config — via
        ``resolved_profile_for``) and composes the in-memory override as ONE MORE restrict-only ∩
        conjunct, then SETs both live fields (never a union — ``apply_per_session_narrowing`` unions
        excluded, so it can't RE-WIDEN; re-resolve-from-base + SET can). Because the override only
        adds deny/exclusion ON TOP of the envelope, a toggle can only HIDE within the authorized set
        — toggle-ON discards from the override so the capability is restored *up to the envelope*,
        never re-granted beyond it (an envelope-denied capability stays denied). The per-turn
        RouterLoop reads these fields at construction, so the change is live next turn.
        """
        from typing import cast

        from reyn.security.permissions.capability_profile import (
            CapabilityProfile,
            compose_resolved,
            resolve_profile,
        )
        from reyn.security.permissions.effective import ContextualPermission
        from reyn.tools.universal_catalog import CATEGORIES

        # resolved_profile_for is documented to return (ContextualPermission | None, ...);
        # its declared type is the wider `object | None`, so cast to the concrete type the
        # downstream compose_resolved requires (registry.py:3509 guarantees it).
        base_ctx: "ContextualPermission | None" = None
        base_excl: "frozenset[str]" = frozenset()
        if self._registry is not None and hasattr(self._registry, "resolved_profile_for"):
            raw_ctx, base_excl = self._registry.resolved_profile_for(
                self._agent_name, sid=self._session_id_provider(),
            )
            base_ctx = cast("ContextualPermission | None", raw_ctx)

        ov = self._visibility_override
        keep_categories: "tuple[str, ...] | None" = None
        if ov["category"]:
            keep_categories = tuple(c for c in CATEGORIES if c not in ov["category"])
        override_profile = CapabilityProfile(
            name="_session_visibility_override",
            tool_deny=tuple(sorted(ov["tool"])),
            mcp_deny=tuple(sorted(ov["mcp"])),
            categories=keep_categories,
        )
        final_ctx, final_excl = compose_resolved([
            (base_ctx or ContextualPermission(), base_excl),
            resolve_profile(override_profile),
        ])
        self._contextual_permission = final_ctx
        self._excluded_categories = final_excl

    def reapply_skill_visibility(self) -> None:
        """#2548 PR-B: recompute the live skill list from the base registered set minus the session override.

        Mutates ``router_host._available_skills`` so the next turn's ``get_available_skills()``
        returns the filtered view. Re-derives from the live ``available_skills_provider()`` (the base
        registered set captured at construction / reapply) so toggle-ON correctly restores a skill —
        it is NOT a union of the current view, which would lose previously-disabled skills."""
        base = self._available_skills_provider() or []
        disabled = self._visibility_override.get("skill", set())
        filtered = [s for s in base if s.name not in disabled]
        self._router_host._available_skills = filtered or None

    def set_capability_visible(
        self, kind: str, name: str, visible: bool, toggle_store_dir: "Path",
    ) -> None:
        """#2285: toggle the session-visibility of a tool / mcp / category / skill (status-bar seam).

        ``visible=False`` hides it from the LLM catalog next turn; ``visible=True`` restores it —
        but only UP TO the agent envelope (toggling ON a capability the envelope denies is a no-op
        for visibility: ``reapply_visibility_override`` re-resolves from base, which still denies
        it). Session-scoped (this sid only); live next turn; persists across restart (step2,
        ``toggle_store_dir`` is the caller's per-session state dir).

        For ``kind="skill"``: restrict-only within the registered set — disabling a skill name not
        in the registered set is silently ignored (no error; the override is a no-op). Enabling a
        skill name not in the registered set is also silently ignored (can never re-grant beyond the
        registered set). ``reapply_skill_visibility`` re-derives the filtered list from the base
        registered set each time."""
        if kind not in self._visibility_override:
            raise ValueError(
                f"unknown capability kind {kind!r} (expected tool / mcp / category / skill)"
            )
        if visible:
            self._visibility_override[kind].discard(name)
        else:
            self._visibility_override[kind].add(name)
        if kind == "skill":
            self.reapply_skill_visibility()
        else:
            self.reapply_visibility_override()
        self.persist_visibility_override(toggle_store_dir)  # #2285 step2 — survive restart (best-effort)

    def _reachable_tool_names(self, excluded_categories: "frozenset[str]") -> "set[str]":
        """#3220: the "tool" census SOURCE — capabilities reachable in the actual
        per-turn composed payload for the active chat-layer scheme, NOT a global
        registry census.

        Ground truth (#3220 issue): the prior source, ``get_default_registry().names()``,
        enumerates every registered ``ToolDefinition`` regardless of whether the active
        scheme's ``build_tools()`` call ever advertises it (a ``gates.router != "allow"``
        tool — e.g. the phase-only ``ask_user`` — is registry-visible but NEVER emitted by
        ``build_tools()`` for ANY scheme) and regardless of scheme shape (``universal-category``
        collapses individual/MCP tool names into 3-4 wrapper meta-tools; ``codeact`` emits no
        ``tools=`` schema at all). This method re-derives the census from the SAME
        ``build_tools()`` / ``universal_catalog.catalog_entries()`` substrate the real
        composition path (``RouterLoop.present`` / ``base_tools`` / ``catalog_entries``, and
        each ``ToolUseScheme.build_presentation``) calls — no reinvented logic.

        Per-scheme granularity (architect-confirmed #3220 firm): a wrapper-folded scheme
        must EXPAND the wrapper back to the reachable capabilities it makes callable, not
        show the opaque wrapper name — the operator sees "the underlying capability is
        usable", not "invoke_action is a tool".

        - ``universal-category``: ``build_tools(universal_wrappers_enabled=True)`` strips
          the legacy per-kind names EXCEPT a few router-only primitives added after the
          strip-list was last updated (``session_spawn`` / ``agent_spawn`` /
          ``topology_create`` — #2120's advertise-drift lesson) — those SURVIVE the
          wrapper-mode build and stay literally in ``tools=``, so they are kept (computed
          as the intersection with the wrappers-off build, no hardcoded name list — a new
          survivor is picked up automatically). Every OTHER capability is reachable only
          THROUGH the ``invoke_action`` wrapper, against the closed ``universal_catalog``
          table — expanded here via ``catalog_entries()``.
        - ``codeact``: ``llm_tools_payload`` is genuinely ``[]`` (no JSON schema at all);
          the model calls actions via the code-API rendered from ``ops.catalog_entries()``
          alone (``CodeActScheme.build_presentation`` never unions ``base_tools()``) — so
          the reachable set is the catalog names only, matching ``dispatchable_catalog``.
        - ``enumerate-all`` / ``retrieval`` / any other registered scheme: both the flat
          legacy names (``build_tools(universal_wrappers_enabled=False)``) AND the catalog
          names are literally present in ``tools=`` (``EnumerateAllScheme.build_presentation``
          unions ``base_tools() + catalog_entries()``) — the safe default for a scheme this
          method does not special-case.

        ``excluded_categories`` (the same conjunct the "category" kind below already
        applies) also drops a whole category's catalog actions here — one source of
        exclusion, not two competing ones.
        """
        from reyn.runtime.router_tools import build_tools
        from reyn.tools import universal_catalog
        from reyn.tools.types import RouterCallerState, ToolContext

        host = self._router_host
        agents = host.list_available_agents()
        file_permissions = host.get_file_permissions()
        mcp_servers = host.get_mcp_servers()
        web_fetch_allowed = host.get_web_fetch_allowed()

        legacy_tools = build_tools(
            agents,
            file_permissions=file_permissions,
            mcp_servers=mcp_servers,
            web_fetch_allowed=web_fetch_allowed,
            universal_wrappers_enabled=False,
        )
        legacy_names = {t["function"]["name"] for t in legacy_tools}

        # #3026/#1667: the catalog's OWN availability gate (excluded_categories, exec's
        # sandbox backend) — router_state carries only what _enumerate_category reads,
        # never events/permission_resolver/workspace (catalog_entries's NAME projection
        # doesn't dereference those), so a minimal RouterCallerState is faithful here.
        tool_ctx = ToolContext(
            events=None,
            permission_resolver=None,
            workspace=None,
            caller_kind="router",
            router_state=RouterCallerState(
                excluded_categories=excluded_categories,
                sandbox_backend=host.get_sandbox_backend(),
            ),
        )
        catalog_names = {entry["name"] for entry in universal_catalog.catalog_entries(tool_ctx)}

        if self._chat_tool_use_scheme == "universal-category":
            universal_tools = build_tools(
                agents,
                file_permissions=file_permissions,
                mcp_servers=mcp_servers,
                web_fetch_allowed=web_fetch_allowed,
                universal_wrappers_enabled=True,
            )
            universal_names = {t["function"]["name"] for t in universal_tools}
            native_survivors = universal_names & legacy_names
            return native_survivors | catalog_names
        if self._chat_tool_use_scheme == "codeact":
            return set(catalog_names)
        return legacy_names | catalog_names

    def capability_visibility_state(self) -> dict:
        """#2285: the status-bar's read model.

        ``authorized`` = every capability the AGENT ENVELOPE permits for this session (topology ∩
        delegate ∩ per-session config, WITHOUT the visibility override) — the full togglable
        universe. ``hidden_by_session`` = the override set (what the user turned OFF). The UI renders
        ``on = item not in hidden_by_session``. authorized is computed from the live catalogs
        (tools / mcp / categories / skills) filtered by the envelope's ``allows`` — so it always
        reflects visible ⊆ authorized (nothing outside the envelope is ever togglable). #3220: the
        "tool" kind is sourced from ``_reachable_tool_names`` — the actual per-turn composed
        ``tools=`` payload for the active scheme (expanded through any wrapper) — not a raw global
        registry census, so a capability absent from every scheme's composed payload (e.g. a
        ``gates.router="deny"`` phase-only tool) is never shown as visible.
        Kind ∈ tool / mcp / category / skill."""
        from typing import cast

        from reyn.security.permissions.effective import (
            CapabilityAxis,
            ContextualLayer,
            ContextualPermission,
        )
        from reyn.tools.universal_catalog import CATEGORIES

        # resolved_profile_for's declared return is the wider `object | None`; cast to the
        # concrete type ContextualLayer expects (registry.py:3509 documents ContextualPermission).
        base_ctx: "ContextualPermission | None" = None
        base_excl: "frozenset[str]" = frozenset()
        if self._registry is not None and hasattr(self._registry, "resolved_profile_for"):
            raw_ctx, base_excl = self._registry.resolved_profile_for(
                self._agent_name, sid=self._session_id_provider(),
            )
            base_ctx = cast("ContextualPermission | None", raw_ctx)
        ctx = ContextualLayer(base_ctx)  # the envelope gate (None → allows all)

        authorized: "list[dict]" = []
        for name in sorted(self._reachable_tool_names(base_excl)):
            if ctx.allows(CapabilityAxis.TOOL, name):
                authorized.append({"kind": "tool", "name": name})
        for server in self._router_host.get_mcp_servers():
            n = server.get("name")
            if n and ctx.allows(CapabilityAxis.MCP, n):
                authorized.append({"kind": "mcp", "name": n})
        for category in CATEGORIES:
            if category not in base_excl:
                authorized.append({"kind": "category", "name": category})
        # #2548 PR-B: skills are togglable per-session; the registered base set is the envelope.
        for entry in (self._available_skills_provider() or []):
            authorized.append({"kind": "skill", "name": entry.name})

        hidden = [
            {"kind": kind, "name": name}
            for kind, names in self._visibility_override.items()
            for name in sorted(names)
        ]
        return {"authorized": authorized, "hidden_by_session": hidden}

    def persist_visibility_override(self, toggle_store_dir: "Path") -> None:
        """#2285 step2: persist the visibility override to ``<state dir>/visibility.yaml`` — a store
        DISTINCT from the config.yaml spawner-narrowing (the authorized floor). Keeping it separate is
        load-bearing: a toggle-ON must never edit the floor's denies (that would re-widen past
        authorized). Best-effort: a write failure logs, never breaks the already-applied live toggle."""
        import logging

        import yaml

        logger = logging.getLogger(__name__)
        try:
            data = {k: sorted(v) for k, v in self._visibility_override.items() if v}
            path = toggle_store_dir / "visibility.yaml"
            if data:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(yaml.safe_dump(data), encoding="utf-8")
            elif path.exists():
                path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 — persist is best-effort (live toggle already applied)
            logger.warning("#2285: persist visibility override failed: %r", exc)

    def load_persisted(self, data: dict) -> "tuple[bool, bool]":
        """#2285 step2: restore a previously-persisted visibility override (parsed from
        ``visibility.yaml``) into the in-memory toggle state. Resets to a clean baseline first so a
        reload fully re-derives from the given data — idempotent + leak-free if called more than
        once. Returns ``(loaded_any, loaded_skill)`` — the caller reapplies
        ``reapply_visibility_override`` when ``loaded_any`` and ``reapply_skill_visibility`` when
        ``loaded_skill`` (mirrors the pre-extraction two-flag behavior exactly: a tool/mcp/category-only
        change does not need the (separate, live-router-mutating) skill reapply, and vice versa)."""
        self._visibility_override = {"tool": set(), "mcp": set(), "category": set(), "skill": set()}
        loaded_any = False
        loaded_skill = False
        if isinstance(data, dict):
            for kind in ("tool", "mcp", "category"):
                vals = data.get(kind)
                if isinstance(vals, list):
                    self._visibility_override[kind] = {str(v) for v in vals}
                    loaded_any = True
            skill_vals = data.get("skill")
            if isinstance(skill_vals, list):
                self._visibility_override["skill"] = {str(v) for v in skill_vals}
                loaded_skill = True
        return loaded_any, loaded_skill
