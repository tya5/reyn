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

from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from pathlib import Path


def _run_coro_sync(coro: Any) -> Any:
    """#3220: run a coroutine to completion from a SYNC call site, without
    changing ``capability_visibility_state()``'s public sync contract (every
    call site — the inline TUI's per-render-frame status snapshot, the REPL /
    AG-UI read models — invokes it as a plain sync accessor; making it ``async``
    would ripple through those frontends for what is a status-bar read).

    Always drives the coroutine on a throwaway event loop in a dedicated thread
    (``asyncio.run`` in a fresh thread, never on the calling thread) — safe
    regardless of whether the calling thread already has a running loop of its
    own (a bare ``asyncio.run()`` here would raise in that case; a TUI's async
    event loop is the expected caller). Cheap in practice: every coroutine this
    module drives through here (``_VisibilityProbeOps``) resolves without a
    true suspension (no real disk/network I/O — see its docstring), so the only
    cost is thread + loop setup, not a blocking wait.
    """
    import asyncio
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class _VisibilityProbeOps:
    """#3220: a minimal ``SchemeOps``-shaped facade providing ONLY the three
    host-derived RAW-INGREDIENT methods every ``ToolUseScheme.build_presentation``
    calls (``present`` / ``base_tools`` / ``catalog_entries``) — the SAME
    ``build_tools()`` / ``universal_catalog.catalog_entries()`` substrate
    ``RouterLoop``'s real ``SchemeOps`` implementation wraps. Calling a scheme's
    REAL (unmodified, imported from ``reyn.tools.schemes.*``) ``build_presentation``
    through this facade reproduces the scheme's OWN composition transform (e.g.
    ``EnumerateAllScheme``'s #3224 post-catalog ``mcp_call_tool`` exclusion)
    instead of re-deriving the final output in parallel and silently drifting the
    moment a scheme adds one of those — the exact bug an architect co-vet caught
    in this fix's first cut.

    Unlike ``RouterLoop.catalog_entries`` (which awaits
    ``build_resource_caller_state(host)`` — a genuine disk read for the RAG
    source-manifest, appropriate mid-turn but wrong for a status-bar snapshot
    called every render frame), ``catalog_entries`` here builds a MINIMAL
    ``RouterCallerState`` carrying only what #3026 documents actually gates the
    NAME set (``excluded_categories`` / ``sandbox_backend``) — every await this
    class performs resolves without a true suspension.

    Only the 3 methods the 4 shipped schemes' ``build_presentation`` bodies
    call are implemented (``search_actions`` is never reached: this facade
    forces ``search_visible=False``, and retrieval's own fallback for that case
    never calls it either)."""

    def __init__(self, router_host: "_RouterHost", excluded_categories: "frozenset[str]") -> None:
        self._host = router_host
        self._excluded_categories = excluded_categories

    def present(self, available: dict, layer_ctx: dict) -> Any:
        from reyn.runtime.router_tools import build_tools
        from reyn.tools.scheme import AdvertisedTools, Presentation

        # This probe stands in for the router's own ``present``, which is always a
        # ``tool_calls`` presentation — the channel exists (#3421).
        return Presentation(tools_channel=AdvertisedTools(entries=build_tools(
            self._host.list_available_agents(),
            file_permissions=self._host.get_file_permissions(),
            mcp_servers=self._host.get_mcp_servers(),
            web_fetch_allowed=self._host.get_web_fetch_allowed(),
            universal_wrappers_enabled=layer_ctx.get("univ_enabled", False),
            search_actions_visible=layer_ctx.get("search_visible", False),
            compact_visible=layer_ctx.get("ctx_signal_present", False),
        )))

    def base_tools(self, available: dict, layer_ctx: dict) -> "list[dict]":
        from reyn.runtime.router_tools import build_tools

        return build_tools(
            self._host.list_available_agents(),
            file_permissions=self._host.get_file_permissions(),
            mcp_servers=self._host.get_mcp_servers(),
            web_fetch_allowed=self._host.get_web_fetch_allowed(),
            universal_wrappers_enabled=False,
            search_actions_visible=False,
            compact_visible=layer_ctx.get("ctx_signal_present", False),
        )

    async def catalog_entries(self) -> "list[dict]":
        from reyn.tools import universal_catalog
        from reyn.tools.types import RouterCallerState, ToolContext

        tool_ctx = ToolContext(
            events=None,
            permission_resolver=None,
            workspace=None,
            caller_kind="router",
            router_state=RouterCallerState(
                excluded_categories=self._excluded_categories,
                sandbox_backend=self._host.get_sandbox_backend(),
            ),
        )
        return [
            {
                "type": "function",
                "function": {
                    "name": entry["name"],
                    "description": entry["description"],
                    "parameters": entry["parameters"],
                },
            }
            for entry in universal_catalog.catalog_entries(tool_ctx)
        ]


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
    def get_universal_wrappers_enabled(self) -> bool: ...


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
        # registry key — "enumerate-all" / "universal-category" / "retrieval" / the
        # three content_fence cells' resolved names: (enumerate-all, content_fence) =
        # CodeAct, FP-0066 P4c #3247, plus (category, content_fence) #3376 P2 and
        # (retrieval, content_fence) #3376 P3).
        # Immutable for the session's lifetime (Session never reassigns
        # ``self._chat_tool_use_scheme`` post-construction — same stability class as
        # ``agent_name``), so a plain field is correct here, not a live provider.
        self._chat_tool_use_scheme = chat_tool_use_scheme
        # Session-scoped LLM tool-VISIBILITY override, restrict-only on top of the resolved agent envelope (#2285)
        self._visibility_override: "dict[str, set[str]]" = {
            "tool": set(), "mcp": set(), "category": set(), "skill": set(),
        }
        # #5276: the expensive envelope-only census's cache — see
        # _envelope_census's own docstring. None = never computed / needs
        # recompute; invalidated synchronously by invalidate_envelope_census
        # — called from Session at 3 sites (grep-confirmed:
        # `git grep invalidate_envelope_census -- src` → session.py's
        # `_reapply_mcp`, `_reapply_skills`, `load_persisted_toggles`) —
        # never via an EventLog subscriber, the #5279/#5284 lesson.
        self._cached_envelope_census: "dict | None" = None

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

        #3593 ① — NO BASE ⇒ NO WRITE. The SET above is correct only when a base was actually
        OBTAINED. Without an envelope source there is nothing to re-resolve, and composing the
        override against a default ``ContextualPermission()`` (which allows everything) and SETting
        THAT paints over live fields that were correct until that moment — the topology
        ``capability_profile`` bindings, the #2081 ``_delegate`` floor and the #2103-S1a
        per-session narrowing all replaced by "allow-all minus whatever the operator toggled".
        A missing INPUT was triggering a WRITE, and the write went outward.

        The live fields already hold the correct values (resolved at construction from a real
        base), so the existing state is the authority and any state derived from a base that
        could not be read is fabricated. This method therefore has no standing to overwrite it
        and returns without writing — that is a judgement about authority, not the "it is safe,
        so do nothing" a *no-op* would name. It is not fail-closed either: fail-closed would be
        another write, of a value nobody resolved.

        Cost of preserving, stated rather than buried: on such a session an override change made
        since the last successful resolve does not reach the live gate (it is still recorded in
        ``visibility_override`` / persisted / rendered in ``hidden_by_session``). Applying it
        anyway would require an envelope to compose against, and inventing one is the defect.
        The condition is surfaced (WARNING) rather than passed over in silence — see
        ``_surface_unreadable_envelope_source``.
        """
        from typing import cast

        from reyn.security.permissions.capability_profile import (
            CapabilityProfile,
            compose_resolved,
            resolve_profile,
        )
        from reyn.security.permissions.effective import (
            ContextualPermission,
            NarrowingOrigin,
        )
        from reyn.tools.universal_catalog import CATEGORIES

        # resolved_profile_for is documented to return (ContextualPermission | None, ...);
        # its declared type is the wider `object | None`, so cast to the concrete type the
        # downstream compose_resolved requires (registry.py:3509 guarantees it).
        #
        # #3593 review: `registry` is genuinely `_EnvelopeSource | None` — Session's own
        # `registry` field is `AgentRegistry | None` (a caller without a back-reference is
        # a real, legitimate state, not a wiring bug to paper over with a non-Optional lie)
        # — so `is None` is a fully-typed discriminator, not a runtime-only guess about a
        # static signal. `not hasattr(..., "resolved_profile_for")` stays alongside it as a
        # SEPARATE, purely defensive check: Python does not enforce the Protocol at
        # runtime, so a non-conforming object could theoretically reach here despite the
        # type. Either arm means the same thing (no base to read), which is why they share
        # one branch: composing the override against an allow-everything default and
        # SETting it would silently replace a persisted `tool_deny` narrowing with ALLOWED
        # — a permission WIDENING, the opposite of fail-closed.
        if self._registry is None or not hasattr(self._registry, "resolved_profile_for"):
            # #3593 ①: preserve — see the docstring. No base was obtained, so nothing below
            # may run: everything below composes a NEW envelope and SETs it over the live one.
            self._surface_unreadable_envelope_source()
            return
        raw_ctx, base_excl = self._registry.resolved_profile_for(
            self._agent_name, sid=self._session_id_provider(),
        )
        base_ctx: "ContextualPermission | None" = cast("ContextualPermission | None", raw_ctx)

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
            resolve_profile(override_profile, origin=NarrowingOrigin(
                label="the session's own `/visibility` override",
                cause="this capability was switched off for this session by the user",
                lifts_when=(
                    "the user switches it back on — `/visibility` in the CUI, or the "
                    "Tool tab toggle. It is the one narrowing the operator can flip "
                    "live"
                ),
            )),
        ])
        self._contextual_permission = final_ctx
        self._excluded_categories = final_excl

    def _surface_unreadable_envelope_source(self) -> None:
        """#3593 ①: say out loud that the envelope base could not be read.

        Preserving is the safe direction, which is exactly why it must not be silent: the
        session keeps whatever gate it already had, so nothing downstream misbehaves, and the
        wiring defect that produced a security-core object with no envelope source leaves no
        other trace. A widening bug announces nothing on its own (#3593's framing); a
        *silently* preserved one announces nothing either.

        WARNING log, not a P6 audit-event kind, and the reason is not convenience:

        - ``.reyn/events`` is the replayable record of what happened to the WORKSPACE, and its
          ``type`` namespace is a closed vocabulary with consumers outside reyn (see
          ``AUDIT_EVENT_KINDS``). "A collaborator this object needed was absent" is a fact
          about how reyn was WIRED at construction, not an action taken on the workspace, and
          it reconstructs nothing on replay.
        - This class holds four deps by design (envelope source, router host, two live
          getters) and no event sink. Injecting one — through ``Session`` and every
          construction site — to carry a single wiring diagnostic is a larger coupling change
          than the defect being fixed, and would have to be threaded through the very
          bootstrap window (the one measured no-back-reference caller) where it is least
          likely to be available.
        - Sibling precedent in this same class: ``persist_visibility_override`` reports its
          best-effort failure the same way.

        Stage ② (#3593) fixed the one measured bootstrap-ordering caller that reached here
        unnecessarily (dogfood's registry cell). This branch stays reachable by DESIGN,
        though: ``registry`` is genuinely ``AgentRegistry | None`` — a session legitimately
        exists without a back-reference in some construction paths — so "no envelope
        source" is not itself a defect to engineer away, only a state that must never widen
        permissions. This warning is what tells an operator it happened, on any path that
        reaches it.
        """
        import logging

        logging.getLogger(__name__).warning(
            "#3593: capability envelope NOT re-resolved for agent %r (sid %r) — no envelope "
            "source to read the base from. The live envelope is PRESERVED, not widened; any "
            "visibility-override change since the last successful resolve is recorded and "
            "persisted but is NOT reflected in the live gate. A session on the security-core "
            "path is expected to carry the registry back-reference: reaching this means a "
            "construction site built one without it.",
            self._agent_name,
            self._session_id_provider(),
        )

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

        "Live next turn" holds for a session that HAS an envelope source. On one that does not,
        ``reapply_visibility_override`` preserves the live envelope rather than recomposing it
        (#3593 ①, see its docstring) — the toggle is still recorded, persisted and reported in
        ``hidden_by_session``, but it does not reach the live gate, and the attempt is logged.

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
        """#3220: the "tool" census SOURCE — capabilities reachable in the ACTUAL
        composed payload the active chat-layer scheme's OWN ``build_presentation``
        produces, NOT a global registry census, and NOT a parallel re-derivation of
        what that composition is believed to do.

        Ground truth (#3220 issue + architect co-vet on the first cut of this fix):
        the prior source, ``get_default_registry().names()``, enumerates every
        registered ``ToolDefinition`` regardless of whether the active scheme's
        composition ever advertises it. A first attempt at this fix called
        ``build_tools()`` + ``universal_catalog.catalog_entries()`` DIRECTLY, in
        parallel with (not through) each scheme's ``build_presentation`` — which
        re-introduced the exact same class of divergence at finer grain: #3224
        made ``EnumerateAllScheme.build_presentation`` EXCLUDE ``mcp_call_tool``
        from its flattened payload (already covered by the native ``call_mcp_tool``
        tool), a transform that lives INSIDE the scheme method, not in the raw
        ``catalog_entries()`` building block — a parallel re-derivation has no way
        to see it and silently drifts every time a scheme adds one.

        The fix: call the REAL, unmodified ``ToolUseScheme.build_presentation``
        (``reyn.tools.scheme.get_scheme``) for the session's configured scheme, via
        ``_VisibilityProbeOps`` — a facade that supplies ONLY the three host-derived
        RAW INGREDIENTS every scheme's ``build_presentation`` calls (``present`` /
        ``base_tools`` / ``catalog_entries``, the same ``build_tools()`` /
        ``universal_catalog.catalog_entries()`` substrate ``RouterLoop``'s real
        ``SchemeOps`` implementation wraps) — so any scheme-owned transform on top
        of those ingredients (like the #3224 exclusion) is captured for free,
        because this calls the actual method body, never a copy of it.

        ``build_presentation`` is ``async def`` on every scheme (P7: schemes can do
        real I/O, e.g. retrieval's dynamic search). ``capability_visibility_state``
        must stay a plain SYNC accessor (every call site — the inline TUI's
        per-render-frame status snapshot, the REPL/AG-UI read models — calls it
        synchronously; making it async ripples through 3+ frontends for a
        status-bar read, well past this fix's scope). ``_run_coro_sync`` bridges
        the two: none of ``_VisibilityProbeOps``'s awaits ever truly suspend (its
        ``catalog_entries`` builds a MINIMAL ``RouterCallerState`` — #3026 documents
        the catalog's NAME set depends only on ``excluded_categories`` /
        ``sandbox_backend``, both already available synchronously here — never the
        real ``build_resource_caller_state(host)`` RouterLoop uses mid-turn, which
        does genuine disk I/O for the RAG source manifest), so the bridge's cost is
        coroutine-scheduling overhead only, not a hidden I/O wait.

        Wrapper-expansion (architect-confirmed granularity) still holds: whatever
        NAMES a scheme's own ``build_presentation`` puts in ``tools_channel``
        (or ``dispatchable_catalog`` when the scheme decouples it — CodeAct) ARE
        the reachable set, by construction — ``universal-category``'s own
        ``present()`` calls ``build_tools(universal_wrappers_enabled=True)``,
        whose payload already IS "the wrapper meta-tools + whatever legacy
        primitives survive the strip"; expanding to the underlying catalog
        capabilities the wrapper makes reachable (not just the opaque wrapper
        name) is this method's OWN addition on top of the composed payload, driven
        by the SAME catalog ingredient the wrapper dispatches against.
        """
        # #4366: the built-in schemes self-register via an IMPORT-TIME side
        # effect (each module under ``reyn.tools.schemes.*`` calls
        # ``register_scheme`` at module scope) -- importing the registry
        # module alone (``reyn.tools.scheme``, singular, below) never
        # triggers it; only importing the built-ins package (``schemes``,
        # plural) or one of its submodules does. In the normal chat path
        # this happens for free the moment ``RouterLoop`` resolves a scheme
        # (``router_loop.py``'s own ``import reyn.tools.schemes`` before its
        # ``get_scheme`` calls, same pattern as here) -- but THIS census can
        # run before the LLM is ever called (a fresh session's first
        # status-bar render), when the router loop has not imported
        # anything yet, so the registry is empty and both ``get_scheme``
        # calls below silently return ``None``. Declaring the dependency
        # explicitly (importing it here too, idempotent) rather than
        # continuing to lean on a side effect nothing in this module ever
        # named is the fix, not a cost -- measured against a real running
        # TUI (#4366 comments): ~30 additional small ``reyn`` modules, the
        # heavy transitive deps (ssl/socket/asyncio/subprocess) already
        # loaded by then, the increment smaller than run-to-run baseline
        # variance.
        import reyn.tools.schemes  # noqa: F401  (register_scheme import-time side effect)
        from reyn.tools.scheme import (
            DEFAULT_SCHEME_NAME,
            advertised_entries,
            flat_catalog_entries,
            get_scheme,
        )

        scheme = get_scheme(self._chat_tool_use_scheme) or get_scheme(DEFAULT_SCHEME_NAME)
        if scheme is None:
            # #4366: the ``or`` above only covers "configured name unknown,
            # fall back to the default" -- it was never a guard against an
            # EMPTY registry (the default lookup fails identically to the
            # configured one in that case). The import above makes an empty
            # registry unreachable in practice; if this still fires, the
            # registry itself is broken (e.g. a built-in scheme's own
            # ``register_scheme`` call was removed) -- an internal
            # invariant violation, not a per-turn/per-config condition, so
            # it is raised loudly here rather than silently degraded to a
            # placeholder census (which would hide exactly that class of
            # bug behind an empty-looking Tool tab).
            raise RuntimeError(
                f"no tool-use scheme registered under "
                f"{self._chat_tool_use_scheme!r} or the default "
                f"{DEFAULT_SCHEME_NAME!r} after importing reyn.tools.schemes "
                "-- the built-in scheme registry is unexpectedly empty."
            )
        ops = _VisibilityProbeOps(self._router_host, excluded_categories)
        univ_enabled = bool(self._router_host.get_universal_wrappers_enabled())
        # #3378: the census is the UN-narrowed reachable set — the contextual is applied
        # by ``capability_visibility_state`` below, which needs the denied rows to
        # RENDER them as denied. Passing the narrowing here instead would delete them
        # from the census and the Tool tab could not say why they are unavailable.
        available = {"contextual_permission": None}
        layer_ctx = {
            "univ_enabled": univ_enabled,
            # search_actions is wrapper plumbing (excluded below regardless of
            # visibility) and retrieval's own search-unavailable fallback path
            # already degrades to "present the full flat catalog" when False —
            # the conservative default for a read-only census (no live embedding
            # search performed for a status-bar snapshot).
            "search_visible": False,
            "ctx_signal_present": False,
            "router_model": None,
            "router_model_family": "other",
            "non_interactive": True,
            "available_skills": None,
        }
        pres = _run_coro_sync(scheme.build_presentation(available, layer_ctx, ops=ops))

        if pres.dispatchable_catalog is not None:
            # A scheme that decouples dispatch from advertisement — every
            # ``content_fence`` cell, whose channel arm is ``NoToolsChannel``, plus
            # any ``tool_calls`` cell that chooses to. The reachable set is the
            # dispatchable catalog, not what is advertised.
            #
            # #3421: this used to read the advertised payload first and then
            # overwrite it, with a comment explaining that the CodeAct payload was
            # "genuinely []". The comment is gone because the type now says it:
            # a ``NoToolsChannel`` presentation cannot reach the ``else`` branch —
            # ``Presentation`` refuses to exist with that arm and no
            # ``dispatchable_catalog``.
            names = {e["name"] for e in flat_catalog_entries(pres.dispatchable_catalog)}
        else:
            names = {
                e["name"]
                for e in flat_catalog_entries(advertised_entries(pres.tools_channel))
            }

        if self._chat_tool_use_scheme == "universal-category":
            # Wrapper-expansion: the composed payload names the 3-4 wrapper
            # meta-tools themselves (list_actions / describe_action /
            # invoke_action[/ search_actions]) — not "capabilities". Drop the
            # plumbing, keep any legacy primitive that survived the wrapper-mode
            # strip (computed generically below, not by hardcoding names), and
            # expand to the underlying catalog capabilities invoke_action makes
            # reachable.
            legacy_names = {
                e["name"] for e in flat_catalog_entries(ops.base_tools(available, layer_ctx))
            }
            catalog_names = {
                e["name"] for e in flat_catalog_entries(_run_coro_sync(ops.catalog_entries()))
            }
            wrapper_plumbing = names - legacy_names - catalog_names
            names = (names - wrapper_plumbing) | catalog_names
        return names

    def invalidate_envelope_census(self) -> None:
        """#5276: mark :attr:`_cached_envelope_census` dirty. Called
        SYNCHRONOUSLY by ``Session`` at each of its 3 grep-confirmed
        mutation sites (``_reapply_mcp``, ``_reapply_skills``,
        ``load_persisted_toggles``) — NOT via an ``EventLog`` subscriber
        (the #5279/#5284 lesson: subscriber dispatch is queued whenever a
        loop is running, #4966, so it cannot reliably invalidate a cache
        before a caller that mutates-then-reads-immediately observes it).
        See :meth:`capability_visibility_state`'s own docstring for what
        this census covers and why it is safe to memoize."""
        self._cached_envelope_census = None

    def _envelope_census(self) -> dict:
        """#5276: the EXPENSIVE, envelope-only half of
        ``capability_visibility_state`` — memoized in
        :attr:`_cached_envelope_census`. Computes, for every reachable
        tool/mcp/category/skill, whether the AGENT ENVELOPE (topology ∩
        delegate ∩ per-session config — never the ``/visibility`` override,
        never the per-turn ephemeral taint) authorizes it — via
        ``_reachable_tool_names``'s real, bridged-sync scheme
        ``build_presentation`` call (#3220) and a fresh
        ``resolved_profile_for`` read, both genuinely non-trivial per-call
        costs this method exists to stop paying every render frame.

        Grep-confirmed (#5276 investigation) invariant this memoization
        relies on: the ENVELOPE itself (topology bindings, the #2081
        delegate floor, #2103-S1a per-session config) is set at THIS
        session's own construction/spawn/restore and never mutated by any
        live, in-session command — ``resolved_profile_for``'s answer for a
        GIVEN (agent, sid) does not change while that session keeps
        running EXCEPT for one edge the ``sid=`` argument itself exposes —
        a session's own sid CAN be re-keyed post-construction (spawn
        fixup; see this class's own module docstring), so ``load_persisted_
        toggles`` (called right after a re-key) is this cache's 3rd
        invalidation site, alongside the two below. What DOES change
        mid-session otherwise, and this cache correctly tracks via
        :meth:`invalidate_envelope_census`'s 3 call sites, is WHICH
        capabilities exist to classify at all: the MCP server roster
        (``_reapply_mcp``) and the skill registry (``_reapply_skills``)
        can both change via hot-reload.

        #5288 (filed, not fixed here): whether some OTHER input the active
        scheme's own ``build_presentation`` reads (e.g.
        ``list_available_agents()``, via ``_VisibilityProbeOps``) can
        change mid-session independent of these 3 sites is not
        grep-verified.

        Returns a dict whose shape mirrors the ORIGINAL method's own 3
        per-kind treatments exactly (preserved verbatim, just relocated —
        see the loop below): ``eph_pending_authorized``/
        ``eph_pending_unknown`` hold TOOL/MCP rows that still need the
        cheap per-turn ephemeral check the caller applies (the only rows
        that ever did, in the pre-#5276 code); ``final_authorized``/
        ``final_unknown`` hold CATEGORY/SKILL rows that are already the
        FINAL answer (categories and skills were never checked against
        the ephemeral gate at all, before or after this change — a
        category is either envelope-authorized or not, full stop; a
        skill is always authorized, full stop). ``denied_by_envelope``
        is final for every kind (the pre-#5276 code never re-checked an
        envelope denial against the ephemeral gate either — an envelope
        denial already IS the actionable answer, #3380's own docstring).
        Getting this 3-way split wrong would silently apply the
        turn-context check to a kind that must never carry it (or vice
        versa) — caught and fixed during this PR's own implementation by
        re-deriving each kind's original branch instead of assuming
        symmetry."""
        from typing import cast

        from reyn.security.permissions.effective import (
            CapabilityAxis,
            ContextualLayer,
            ContextualPermission,
        )
        from reyn.tools.universal_catalog import CATEGORIES

        if self._cached_envelope_census is not None:
            return self._cached_envelope_census

        base_ctx: "ContextualPermission | None" = None
        base_excl: "frozenset[str]" = frozenset()
        envelope_unknown = not (
            self._registry is not None and hasattr(self._registry, "resolved_profile_for")
        )
        if not envelope_unknown:
            raw_ctx, base_excl = self._registry.resolved_profile_for(
                self._agent_name, sid=self._session_id_provider(),
            )
            base_ctx = cast("ContextualPermission | None", raw_ctx)
        else:
            self._surface_unreadable_envelope_source_for_read()
        ctx = ContextualLayer(base_ctx)  # the envelope gate (None → allows all)

        eph_pending_authorized: "list[dict]" = []
        eph_pending_unknown: "list[dict]" = []
        denied: "list[dict]" = []
        final_authorized: "list[dict]" = []
        final_unknown: "list[dict]" = []

        def _place(axis: "CapabilityAxis", row: dict) -> None:
            if envelope_unknown:
                eph_pending_unknown.append({**row, "_axis": axis})
            elif not ctx.allows(axis, row["name"]):
                denied.append(row)
            else:
                eph_pending_authorized.append({**row, "_axis": axis})

        for name in sorted(self._reachable_tool_names(base_excl)):
            _place(CapabilityAxis.TOOL, {"kind": "tool", "name": name})
        for server in self._router_host.get_mcp_servers():
            n = server.get("name")
            if n:
                _place(CapabilityAxis.MCP, {"kind": "mcp", "name": n})
        for category in CATEGORIES:
            if envelope_unknown:
                final_unknown.append({"kind": "category", "name": category})
            elif category not in base_excl:
                final_authorized.append({"kind": "category", "name": category})
        # #2548 PR-B: skills are togglable per-session; the registered base set is the
        # envelope — never gated by resolved_profile_for, so #3615's envelope_unknown
        # does not apply here (unaffected by whether the base could be read).
        for entry in (self._available_skills_provider() or []):
            final_authorized.append({"kind": "skill", "name": entry.name})

        self._cached_envelope_census = {
            "eph_pending_authorized": eph_pending_authorized,
            "eph_pending_unknown": eph_pending_unknown,
            "denied_by_envelope": denied,
            "final_authorized": final_authorized,
            "final_unknown": final_unknown,
            "envelope_unknown": envelope_unknown,
        }
        return self._cached_envelope_census

    def capability_visibility_state(
        self, *, ephemeral_contextual: "object | None" = None,
    ) -> dict:
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
        Kind ∈ tool / mcp / category / skill.

        #3378 — ``denied_by_envelope``: reachable capabilities the ENVELOPE contextual
        denies (topology binding / delegate floor / per-session config / ⊆-parent cap).
        These used to be silently dropped from the census, so the Tool tab could not
        distinguish "this tool does not exist here" from "this tool exists but your
        profile denies it" — the owner's "I look at the tab and cannot tell". They are
        a DIFFERENT AXIS from ``hidden_by_session``: the session override is
        user-flippable via ``/visibility``, whereas an envelope denial is not (toggling
        ON re-resolves from base, which still denies) — so a renderer must keep the two
        distinguishable and must not offer a toggle for a ``denied_by_envelope`` row.

        #3380 — ``denied_by_turn_context``: a THIRD list, for what the ``ephemeral_contextual``
        argument denies while the envelope allows it. That argument is
        ``Session._ephemeral_contextual_for_turn()`` — the same ``_untrusted`` term
        ``_effective_contextual_for_turn`` composes for the live gate, passed in rather
        than re-derived here (this class holds the envelope and the override, not the
        conversation whose taint produces it). Kept OUT of ``denied_by_envelope``
        because the two answer different operator questions: an envelope denial is
        durable and un-liftable from the session, while this one lifts itself once the
        untrusted entry compacts out of the active context. A renderer that merged them
        would state a lasting fact about a transient one.

        ``ephemeral_contextual=None`` (the default, and every non-``Session`` caller) →
        ``denied_by_turn_context`` is empty and the other three keys are unchanged.

        #3615 — ``envelope_unknown`` / ``unknown``: the READ-model twin of #3593. When
        there is no envelope source to read the base from (no ``registry`` back-reference
        — the same condition ``reapply_visibility_override`` preserves rather than
        widens on), a legitimate ``base_ctx is None`` (declared: "no narrowing layer",
        ``resolved_profile_for``'s own documented ``(None, frozenset())``-when-no-layer
        return) is INDISTINGUISHABLE, downstream, from "the base could not be
        determined" — both compose against ``ContextualLayer(None)``, which is ⊤ (allows
        everything). Reporting the latter as ``authorized`` is exactly the write-side
        defect's read-model shape: an absent input rendered as a permissive answer.
        Every tool/mcp/category row that would otherwise be classified authorized/denied
        is placed in ``unknown`` instead (skills are the one kind never envelope-gated
        here — see the loop below — so they are unaffected), ``denied_by_envelope`` is
        empty (it cannot be honestly answered without a base), and ``envelope_unknown``
        is ``True`` so a consumer can render "cannot determine" rather than silently
        reading an empty denial list as "nothing is denied". Surfaced loudly (WARNING),
        the same discipline as the write side.

        ``denied_by_turn_context`` is the one exception: it composes ONLY
        ``ephemeral_contextual`` (a caller-supplied argument, unrelated to
        ``self._registry``), so it stays fully determined even when the envelope is
        unknown — a row the ephemeral gate denies is reported there, not swept into
        ``unknown``, because that denial is a fact regardless of what the envelope
        would have said.

        #5276: the EXPENSIVE envelope-only classification (this method used
        to redo it on every call, including every render frame) is memoized
        in :meth:`_envelope_census` — see that method's own docstring for
        the invariant this relies on and its 3 grep-confirmed invalidation
        sites. What runs HERE, on every call, is only the CHEAP overlay:
        splitting each already-envelope-classified row against the live
        ``ephemeral_contextual`` (a plain ``ContextualLayer.allows`` check,
        no catalog work) and reading ``self._visibility_override`` fresh
        (a dict already held in memory) for ``hidden_by_session``. Neither
        of those may be cached — #3380's whole point is that the ephemeral
        taint self-clears the instant the tainted entry compacts out, and
        the override changes on every ``/visibility`` toggle."""
        from typing import cast

        from reyn.security.permissions.effective import ContextualLayer, ContextualPermission

        census = self._envelope_census()
        envelope_unknown = census["envelope_unknown"]
        # #3380: the ephemeral gate, asked ONLY for what the envelope already allows —
        # so a capability the envelope denies keeps its durable reason even while the
        # context happens to be tainted (both deny it; the un-liftable one is the
        # actionable answer).
        eph = ContextualLayer(
            cast("ContextualPermission | None", ephemeral_contextual)
        )

        # #5276: only TOOL/MCP rows (`eph_pending_*`) were ever checked
        # against the ephemeral gate, before or after this change —
        # CATEGORY/SKILL rows (`final_*`) pass straight through unchanged,
        # exactly like the pre-#5276 code's own category/skill loops
        # (neither ever called `eph.allows` at all).
        authorized: "list[dict]" = [
            {"kind": row["kind"], "name": row["name"]}
            for row in census["final_authorized"]
        ]
        denied_turn: "list[dict]" = []
        unknown: "list[dict]" = [
            {"kind": row["kind"], "name": row["name"]}
            for row in census["final_unknown"]
        ]

        for row in census["eph_pending_authorized"]:
            if eph.allows(row["_axis"], row["name"]):
                authorized.append({"kind": row["kind"], "name": row["name"]})
            else:
                denied_turn.append({"kind": row["kind"], "name": row["name"]})
        for row in census["eph_pending_unknown"]:
            # #3615: no base to test the ENVELOPE axis against — classifying into
            # authorized/denied_by_envelope would be a guess dressed as an answer.
            # The TURN-CONTEXT axis is independent of the envelope source (``eph``
            # composes only ``ephemeral_contextual``, a caller-supplied argument
            # that has nothing to do with ``self._registry``), so a turn-context
            # denial is still a determined fact and must not be swallowed into
            # "unknown" — only the row's envelope-standing is undetermined.
            if eph.allows(row["_axis"], row["name"]):
                unknown.append({"kind": row["kind"], "name": row["name"]})
            else:
                denied_turn.append({"kind": row["kind"], "name": row["name"]})

        hidden = [
            {"kind": kind, "name": name}
            for kind, names in self._visibility_override.items()
            for name in sorted(names)
        ]
        return {
            "authorized": authorized,
            "hidden_by_session": hidden,
            "denied_by_envelope": [
                {"kind": row["kind"], "name": row["name"]}
                for row in census["denied_by_envelope"]
            ],
            "denied_by_turn_context": denied_turn,
            "unknown": unknown,
            "envelope_unknown": envelope_unknown,
        }

    def _surface_unreadable_envelope_source_for_read(self) -> None:
        """#3615: the READ-model twin of ``_surface_unreadable_envelope_source`` —
        say out loud that ``capability_visibility_state`` could not determine the
        envelope, rather than let the resulting "everything reported as authorized"
        pass without a trace. Same WARNING-not-audit-event reasoning as the write
        side (a wiring fact about construction, not a workspace action; see that
        method's docstring) — kept as a separate method (not a shared call) because
        the two report DIFFERENT observable consequences (preserved live gate vs. an
        ``unknown`` read-model bucket) and a future edit to one message must not
        silently also change the other's wording."""
        import logging

        logging.getLogger(__name__).warning(
            "#3615: capability_visibility_state() could not resolve the envelope for "
            "agent %r (sid %r) — no envelope source to read the base from. Reporting "
            "these rows as UNKNOWN, not authorized: the absence of a base is not "
            "evidence of an unrestricted envelope. A session on the security-core path "
            "is expected to carry the registry back-reference: reaching this means a "
            "construction site built one without it.",
            self._agent_name,
            self._session_id_provider(),
        )

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
