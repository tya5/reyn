"""Client-side chat read-model — the ADR-0039 P3 seam that makes the inline CUI
transport-agnostic (local ≡ remote by construction, at the RENDERER layer).

The inline input driver (historically a prompt_toolkit inline TTY app, since
retired in favour of the Textual chat app in
:mod:`reyn.interfaces.inline.textual_chat`) renders a live status bar and an
intervention region. P1/P2 already
unified the client's WRITE side behind
:class:`~reyn.interfaces.transport.client_transport.ClientTransport` and its READ
of the conversation/working-indicator stream behind the frame stream — but the
inline driver's *status-panel* reads still went straight to the local
``AgentRegistry`` / ``Session`` by duck-typing (fine in-process, impossible for a
remote client that holds no session). That was the last local-only coupling, and
the inline app's own docstring named it: *"a full client-side read-model is P3."*

This module is that read-model: one seam the inline driver reads ALL of its
status/region state from, with two implementations —

- :class:`RegistryReadModel` — LOCAL, byte-identical to the pre-P3 behavior:
  every accessor delegates to the exact ``_snapshot(registry, …)`` /
  ``registry.attached_session()`` calls the driver made inline before.
- :class:`RemoteReadModel` — REMOTE, backed by the P2
  :class:`~reyn.interfaces.transport.agui.state.RemoteStatusView` the
  ``AgUiTransport`` already populates from the server's ``STATE_*`` stream. It
  projects the wire status subset into the ``_snapshot`` dict shape the chips
  read (:func:`project_remote_snapshot`).

**Frame-sufficiency (what a remote client CAN show).** The server projects only
the MAIN-bar subset onto the wire (``state.py``'s ``project_status`` — the
sole declaration of the wire vocabulary, #5098):
``model`` · ``attached_name`` · ``cost_agent`` / ``cost_total``
/ ``agent_tokens`` · ``ctx_used`` / ``ctx_window`` · ``waiting_on`` ·
``pending_intervention_head`` (#5050). Those chip VALUES, and now the pending
intervention head itself, are genuinely readable via :meth:`RemoteReadModel.
intervention_head` rather than an unconditional None (#5050 closed the
#4996-family lying-None this method carried — see its own docstring; whether
a remote-side RENDERING surface actually consumes this yet is separate and
untouched by #5050, tracked in its own issue thread). The dropdown EXPANSIONS
(cost/ctx detail tuples, the ``/model`` class picker, the agent/session tree,
the ``…`` sub-bar toggle counts), the interactive rewind PICKER, and the
persisted past-turn CONVERSATION log (``conversation_history`` — the #3273
Phase-5 restore-on-restart source) are session-local and are NOT on the wire —
the remote model returns empty/``—``/0 for them (graceful degrade), never a fake
value.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import TYPE_CHECKING

from reyn.interfaces.repl.status import _snapshot

if TYPE_CHECKING:
    from reyn.data.skills.registry import SkillEntry
    from reyn.interfaces.transport.client_transport import ClientTransport
    from reyn.runtime.chat_message import ChatMessage


@dataclass(frozen=True)
class CompletionSourceSnapshot:
    """The completion popup's (#3354) sources, as PLAIN VALUES — never the
    live ``Session`` itself (#5044/#4995, architect ruling on #5044,
    issuecomment-5378399712/issuecomment-5378442342 — "the same root as
    #5079: sync, I/O-performing, Session-mutating does not fit the
    async-marshal shape; but the DIFFERENT answer here is that
    ``completion_source()`` returning a LIVE ``Session`` reference is the
    actual problem, not a threading one — completion needs candidate
    strings (+ freshness), the worker updates, the UI only reads").

    Each field is exactly what ONE slash-command ``CompleterFn`` or the
    ``:`` skill completer reads off the live ``Session`` today (read all 6
    registered completers to confirm, #5044 measurement comment) — no
    field carries a live method or a mutable collection a caller could
    corrupt the source with:

    - :attr:`agent_names` — ``/attach``'s candidate list
      (``session._registry.list_active_names()``).
    - :attr:`known_model_classes` — ``/model``'s candidate list
      (``session.known_model_classes()``), config-static for the
      session's lifetime.
    - :attr:`active_intervention_ids` — ``/answer``'s candidate list
      (``session._interventions.list_active()``) — the ONE genuinely
      per-turn-MUTABLE source among the six; every other field here is
      either session-independent or effectively static per session.
    - :attr:`workspace_dir` — ``/memory``'s own filesystem read is keyed
      by this Path; the completer still does its OWN disk I/O, this just
      supplies the root it reads from instead of a live ``Session`` to
      derive it from.
    - :attr:`available_skills` — the ``:`` skill completer's source, the
      SAME list :meth:`~reyn.runtime.session.Session.available_skills`
      already returns as a copy.

    ``/image``'s path completer is UNAFFECTED — it never reads ``session``
    at all (filesystem completion is session-independent, its own
    docstring already says so), so it needs no field here.

    :meth:`RegistryReadModel.completion_source` (today's only production
    implementation, no thread boundary) builds this synchronously, on
    demand, straight off the live ``Session`` it already holds — always
    current, since there is no cross-thread delay in that path. A future
    threaded implementation (#5048, still blocked on this class landing)
    would instead push the SAME snapshot shape into
    :class:`~reyn.interfaces.transport.threaded._ThreadedSnapshot` at the
    existing frame-refresh cadence — this dataclass is what such a
    producer would refresh, not a second mechanism alongside it.
    """

    agent_names: "tuple[str, ...]" = ()
    known_model_classes: "tuple[str, ...]" = ()
    active_intervention_ids: "tuple[str, ...]" = ()
    workspace_dir: "Path | None" = None
    available_skills: "tuple[SkillEntry, ...]" = ()


def completion_source_snapshot_from_session(session) -> CompletionSourceSnapshot:
    """Build a :class:`CompletionSourceSnapshot` off a real, live
    ``Session`` — the ONE place this conversion happens, so
    :class:`RegistryReadModel` and any other ``ChatReadModel``
    implementation that genuinely holds a local ``Session`` (including a
    test's own real-seam double, e.g. ``test_3354``'s ``SessionReadModel``)
    share it rather than each re-deriving the same field list."""
    registry = getattr(session, "_registry", None)
    return CompletionSourceSnapshot(
        agent_names=tuple(registry.list_active_names()) if registry is not None else (),
        known_model_classes=tuple(session.known_model_classes()),
        active_intervention_ids=tuple(
            iv.id for iv in session.interventions.list_active()
        ),
        workspace_dir=session.workspace_dir,
        available_skills=tuple(session.available_skills()),
    )


@dataclass(frozen=True)
class ChatReadModelCapabilities:
    """A :class:`ChatReadModel` implementation's declared support for its own
    degradable reads (#4996, architect design, owner-directed: "capability
    declaration → issue → leader").

    ``None``/``[]``/``0``/``False`` are the CORRECT graceful-degrade answer
    for every read below when a caller genuinely has nothing to show — this
    declaration does not change any of those return values. What it fixes is
    the caller's side: a bare ``None`` is used identically for "nothing to
    show right now" and "this client can never show this", and the two read
    as the same thing to the TUI. This is the DECLARATION half of making
    that distinction askable, exactly the same discipline
    :class:`~reyn.security.sandbox.backend.AxisEnforcementDeclaration` and
    :mod:`~reyn.core.dispatch.content_declarations` already use — a 3rd
    example, not a new concept.

    Every field is REQUIRED, no defaults anywhere — mirrors
    ``AxisEnforcementDeclaration``'s own discipline verbatim: a NEW
    :class:`ChatReadModel` implementation that forgets a field fails to
    CONSTRUCT its declaration, a ``TypeError`` at the module's own
    construction site, not a silent "unsupported" nobody chose.

    One field per degradable read, named identically to the method it
    describes. ``True`` = this implementation can genuinely produce a
    non-degraded answer; ``False`` = every call always degrades, regardless
    of underlying state (the wire-frame-sufficiency boundary those methods'
    own docstrings already document).

    ``cache_usage_reported`` (#5009, architect co-vet) is the one field
    NOT named after a :class:`ChatReadModel` method — it covers two
    ``snapshot()`` dict KEYS instead (``session_cached_tokens`` /
    ``ctx_recent_usage``), which carry the identical undeclared-``0``
    conflation this class exists to close, just one level down (inside
    the dict a supported METHOD returns, rather than the method itself).
    First tried as a hand-typed literal directly in the two ``snapshot()``
    producers (``status.py`` / ``project_remote_snapshot``) — measured to
    be the WRONG container: a hand-typed literal can be forgotten
    silently, with no construction-time failure to catch it, exactly the
    defect this class exists to prevent. Declaring it HERE instead means
    a producer that forgets it fails to construct, like every other
    field — see :func:`project_remote_snapshot` and ``status.py``'s
    ``_snapshot()`` for how each producer reads its own value from here
    rather than typing the literal a second time.

    **Witness① scope, stated honestly (lead-coder/architect co-vet on
    #4996):** this dataclass's "forgets a field fails to construct"
    guarantee covers exactly ONE failure mode — a NEW
    :class:`ChatReadModel` implementation that omits one of these
    already-declared fields. It is NOT a 1:1 gate over every
    :class:`ChatReadModel` method: ``snapshot()`` and ``history_path()``
    work identically on a remote client and correctly have NO field
    here — a mechanical "one field per abstract method" mapping would be
    a false invariant, not a stronger one. If a further degradable
    method or snapshot key is ever added, THIS dataclass will not fail
    to construct until a human also adds a field for it here; that step
    is not, and cannot be, machine-enforced by this class alone.

    **A DIFFERENT gap this class does NOT close** (#5009's own
    falsified first attempt, kept here as the record): a *snapshot dict*
    that is genuinely ``None``/``{}`` (no read yet — e.g. before mount's
    first frame) and a *snapshot dict missing a declared key* both
    normalize to the same empty shape at a naive consumer that does
    ``snap = snap or {}`` before indexing — bare-indexing the resulting
    dict cannot tell "nothing to show yet" from "an implementation
    forgot to populate this key from its own declaration". This
    dataclass closes the SECOND gap (a producer that forgets to consult
    its own declaration now fails to construct); the FIRST is a
    consumer-side concern the reading pane itself must still handle
    (``snap.get(key, False)`` — false, never true, is the safe direction
    when there is nothing to consult at all).

    **A declaration is an assertion, not an observation** (architect
    co-vet, #5000 — the same limitation ``AxisEnforcementDeclaration``
    already carries, not new here): nothing checks that
    :class:`RegistryReadModel` returning ``LOCAL_CHAT_READ_CAPABILITIES``
    stays TRUE of its actual accessors. If a future edit made
    ``completion_source()`` start returning ``None`` under some new local
    condition without updating this declaration, the declared/actual gap
    this class exists to prevent would reopen silently, on the LOCAL side
    this time. Follow-up, not a defect in this class.

    **The real criterion, corrected** (#5009 closing pass — ``cron_jobs_
    reported`` / ``usage_breakdown_reported`` / ``ctx_compaction_
    reported``): the FIRST measuring stick tried for "does a key need a
    field here" was "is the degrade value indistinguishable from a
    genuinely empty local state" — a useful PROXY, but not the actual
    rule. #4996's own words already said the real one: "never a
    fabricated turn", "never a fabricated count". ``ctx_compaction_
    reported`` is the falsifying case: remote's degraded compaction line
    reads "0% to trigger" — a real local session essentially never has a
    genuine zero-trigger state, so the OLD proxy would have said "no
    conflation, skip it" — but "0%" fabricates reassurance regardless of
    whether a real empty state could produce the same string. The
    correct question is always the fabrication one; "indistinguishable
    from empty" is one way a value fabricates, not the definition of
    fabrication.

    ``hooks_reported``/``pipelines_reported`` (#5034): found while working
    #5025's own thread, in the same file. #5009's original 9-key list was
    HAND-enumerated by architect and missed both — confirmed on #5034 by
    mechanically AST-walking :func:`project_remote_snapshot`'s own return
    dict for every literal (non-wire-sourced) entry, rather than trusting
    the earlier hand count a second time. ``hooks``/``hook_items`` (one
    pane, `_hook_pane_entries`) and ``pipelines`` (`pipe_pane_lines`) both
    degrade to a bare ``["(none)"]`` on remote — byte-identical to a
    genuinely empty local hook/pipeline config, the same fabrication
    ``cron_jobs_reported`` already closed for the Cron pane. The
    mechanical sweep also cleared 6 more literal keys as NOT fabricating
    (``skills``, ``visibility_items``, ``mcp_subscriptions``,
    ``unknown_config_key_count``, ``unknown_config_keys``, ``ctx_source``)
    — see #5034's own issue thread for the per-key reasoning; not
    declared here because there is nothing to gate.

    #5185 REVERSES #5034's clearance for 2 of those 6 —
    ``visibility_items``/``mcp_subscriptions``. #5034's reasoning held for
    the WIRE SHAPE at the time (both were hand-typed ``None``/``[]``
    literals, and #5034 only asked "is the literal indistinguishable from
    a genuinely empty local state" — for ``visibility_items`` the answer
    was already "no", since #3378 made it ``None`` specifically so it
    would NOT read as empty). What #5034 did not (and #5094's precedent
    later showed matters): a hand-typed literal that never changes is
    fabrication regardless of WHICH value it picks, once real per-session
    data exists server-side and is simply never forwarded — owner
    live-observed a real remote MCP pane showing "(not wired)" for a
    session whose subscriptions were genuinely live (#5185). Both fields
    now ride the wire for real (:func:`~reyn.interfaces.transport.agui.
    state.project_status`), declared here so a producer that stops
    forwarding either one fails to construct its declaration rather than
    silently reverting to the old always-empty/always-None shape.
    ``visibility_items_reported`` covers a SHARED key (tool/mcp/skill
    panes all read it via ``chrome.py``'s ``_visibility_pane_rows``) —
    genuinely one flag, since local and remote either both have the wire
    channel or neither does; there is no way for one pane's visibility
    read to be wired while another's is not. ``mcp_subscriptions_reported``
    is its own separate flag (a different key, a different pane-local
    read) even though #5185's acceptance criteria require the two land in
    the SAME PR — landing together is an ORDERING requirement (a
    subscription row has nothing to attach to without ``visibility_items``
    supplying the server row first, see ``_mcp_pane_entries``'s own
    docstring), not a reason to collapse them into one flag.
    """

    completion_source: bool
    intervention_head: bool
    pending_command_ui: bool
    has_command_ui_region: bool
    conversation_history: bool
    load_older_conversation_history: bool
    cache_usage_reported: bool
    cron_jobs_reported: bool
    usage_breakdown_reported: bool
    ctx_compaction_reported: bool
    hooks_reported: bool
    pipelines_reported: bool
    # #5094: 3 more snapshot-key groups found the SAME way #5034 found
    # ``hooks_reported``/``pipelines_reported`` — a hand-typed `[]`/`None`
    # literal in ``project_remote_snapshot`` with no field here to
    # distinguish "genuinely empty" from "this producer can't report it".
    # Unlike the earlier 2, these were ALREADY WRONG on `main` (a real,
    # owner-measured blank remote agent tab), not merely undeclared —
    # ``agent_roster_reported``/``model_catalog_reported``/
    # ``attached_name_reported`` are declared here at the SAME time the
    # underlying wire gap is fixed, so there is no window where the
    # declaration exists but the value is still fabricated.
    agent_roster_reported: bool  # covers `agent_names` + `session_tree` together
    model_catalog_reported: bool  # covers `model_classes` + `model_active_class` together
    attached_name_reported: bool
    # #5185: see the class docstring's own section for why these reverse
    # #5034's clearance and why they stay 2 fields despite landing together.
    visibility_items_reported: bool  # covers `visibility_items` (tool/mcp/skill panes)
    mcp_subscriptions_reported: bool  # covers `mcp_subscriptions` (mcp pane only)


def reported_snapshot_keys(
    capabilities: ChatReadModelCapabilities,
) -> "dict[str, bool]":
    """The FULL ``snapshot()`` dict fragment for *every* field of
    *capabilities* — the ONE source both ``snapshot()`` producers
    (``status.py``'s ``_snapshot()`` and :func:`project_remote_snapshot`)
    merge in, instead of each hand-typing a literal a second time (#5009).

    Generalized (#5009 closing pass, architect co-vet) from 4 near-
    identical single-field helpers — ``cache_usage_reported_snapshot_
    key``, ``cron_jobs_reported_snapshot_key``,
    ``usage_breakdown_reported_snapshot_key``,
    ``ctx_compaction_reported_snapshot_key`` — each a literal copy of the
    same one-line ``return {"<name>": capabilities.<name>}`` shape.
    Measured: 4 helpers meant 4 producer-side call sites too, and the
    NEXT declared field would add a 5th of each — the exact "forgot to
    wire one side" risk this whole design exists to close, just moved
    from the VALUE to the WIRING. One generic projection over every
    dataclass field removes the wiring step entirely: declaring a field
    HERE is now the only step a future key needs; no producer edit, no
    new helper.

    Deliberately projects EVERY field, not only the ``*_reported`` ones
    — the METHOD-axis fields from #4996 (``completion_source`` etc.)
    ride along too, harmlessly (no pane reads them off the snapshot
    dict; ``ChatReadModel.capabilities`` remains their real consumer).
    Simpler than filtering by name pattern, and the field NAME already
    doubles as the snapshot key by construction — the two can no more
    drift apart than a dataclass field can rename itself."""
    return {f.name: getattr(capabilities, f.name) for f in dataclass_fields(capabilities)}


#: :class:`RegistryReadModel` is local — every degradable read reflects real,
#: current state; ``None``/``[]``/``0`` from it always means "genuinely
#: nothing", never "unsupported here".
LOCAL_CHAT_READ_CAPABILITIES = ChatReadModelCapabilities(
    completion_source=True,
    intervention_head=True,
    pending_command_ui=True,
    has_command_ui_region=True,
    conversation_history=True,
    load_older_conversation_history=True,
    cache_usage_reported=True,
    cron_jobs_reported=True,
    usage_breakdown_reported=True,
    ctx_compaction_reported=True,
    hooks_reported=True,
    pipelines_reported=True,
    agent_roster_reported=True,
    model_catalog_reported=True,
    attached_name_reported=True,
    visibility_items_reported=True,
    mcp_subscriptions_reported=True,
)

#: :class:`RemoteReadModel` — the frame-sufficiency boundary each of these
#: methods' own docstrings already document (session-local state the AG-UI
#: wire does not project): every one of them always degrades, independent of
#: server-side state — with exceptions: ``intervention_head`` (#5050):
#: STATE_SNAPSHOT/STATE_DELTA now carry ``pending_intervention_head``, so
#: this one genuinely reflects live server-side state rather than always
#: degrading; and ``agent_roster_reported``/``model_catalog_reported``/
#: ``attached_name_reported`` (#5094, the same fix): those 5 snapshot keys
#: now ride the SAME wire channel, so a remote agent tab / model-class
#: picker reflects the server's real roster instead of an unconditional
#: empty list. ``visibility_items_reported``/``mcp_subscriptions_reported``
#: (#5185, the same fix again): a remote MCP/tool/skill pane now reflects
#: the server's real visibility/subscription state instead of an
#: unconditional "(not wired)"/empty subscription list. See the module
#: docstring's "Frame-sufficiency" section.
REMOTE_CHAT_READ_CAPABILITIES = ChatReadModelCapabilities(
    completion_source=False,
    intervention_head=True,
    pending_command_ui=False,
    has_command_ui_region=False,
    conversation_history=False,
    load_older_conversation_history=False,
    cache_usage_reported=False,
    cron_jobs_reported=False,
    usage_breakdown_reported=False,
    ctx_compaction_reported=False,
    hooks_reported=False,
    pipelines_reported=False,
    agent_roster_reported=True,
    model_catalog_reported=True,
    attached_name_reported=True,
    visibility_items_reported=True,
    mcp_subscriptions_reported=True,
)


class ChatReadModel(ABC):
    """The inline CUI's sole READ seam: status snapshot + region + history.

    Writes still ride the :class:`ClientTransport`; this is the read half the
    inline driver used to take off the registry directly. Abstract (not a bare
    Protocol) so a partial implementation fails at construction, not first use —
    the same completeness-by-construction discipline ``ClientTransport`` uses.
    """

    @property
    @abstractmethod
    def capabilities(self) -> ChatReadModelCapabilities:
        """This implementation's :class:`ChatReadModelCapabilities` — see
        that class's own docstring (#4996). Abstract for the same reason
        every OTHER accessor here is: a new implementation that forgets to
        declare fails to construct, rather than silently reading every
        capability as unsupported."""

    @abstractmethod
    def snapshot(self, config=None) -> "dict | None":
        """Return the status-bar snapshot dict (the ``_snapshot`` shape), or None
        when there is nothing to show (no attached session locally)."""

    @abstractmethod
    def intervention_head(self) -> "object | None":
        """The head closed-set intervention (with ``.id`` / ``.choices``) for the
        above-input region selector, or None.

        LOCAL returns the real ``UserIntervention`` object (``.id``/
        ``.choices`` as real attributes). REMOTE returns the JSON-safe wire
        projection (``{id, prompt, detail, choices: [{id, label, hotkey},
        ...]}``) read off ``pending_intervention_head`` on the transport's
        STATE_SNAPSHOT/STATE_DELTA-fed status view — a DICT, not an object
        with attributes.

        #5050: previously an unconditional None here, regardless of
        server-side state — the #4996-family lying-None pattern
        (``ChatReadModelCapabilities``'s own docstring names it): "never
        supported" and "nothing pending right now" rode the identical
        ``None``, indistinguishable to a caller. This was true independent
        of whether choices themselves reach a remote client some OTHER way
        (they do, via a separate AG-UI frontend-tool encoding — #5050's own
        issue thread; NOT what this method's prior None was hiding). Fixing
        this source is a precondition for any remote consumer of THIS method
        to ever tell the two states apart, whether or not one is wired up
        yet (``REMOTE_CHAT_READ_CAPABILITIES.intervention_head`` is the
        companion declaration — see its own comment)."""

    @abstractmethod
    def pending_command_ui(self) -> "dict | None":
        """The pending command-UI request (the ``/rewind`` picker), or None.
        Command-UI is inline-app-local state, not on the wire → None for remote."""

    @property
    @abstractmethod
    def has_command_ui_region(self) -> bool:
        """Whether this client hosts the interactive command-UI region. False for
        remote → the ``__rewind_list__`` frame renders as a text list instead of
        being swallowed for a picker that will never appear."""

    @property
    @abstractmethod
    def history_path(self) -> Path:
        """Filesystem path for the input-history file."""

    @abstractmethod
    def conversation_history(
        self,
        *,
        limit: "int | None" = None,
        agent: "str | None" = None,
        session_id: "str | None" = None,
    ) -> "list[ChatMessage]":
        """Recent persisted CONVERSATION turns (the ``ChatMessage`` log loaded
        from ``history.jsonl``), oldest→newest, for the Textual app's
        restore-on-restart hydration (#3273 Phase 5) AND its session-switch
        reset-and-rehydrate (#3310 N2 — the same seam, generalized to target
        an arbitrary session rather than only "whichever one is currently
        attached").

        This is the DURABLE conversation log (assistant text + tool results), NOT
        the input-history file :attr:`history_path` (↑-recall) and NOT the P6
        audit-event log (which carries neither and rotates). ``limit`` caps to the
        most-recent N turns; ``None`` returns the whole loaded log (resume-
        equivalent — whatever survives in ``history.jsonl``; turns rotated out are
        simply not restored, exactly like ``--resume``).

        ``agent`` / ``session_id`` (#3310 N2): when BOTH are omitted (``None``),
        behavior is BYTE-IDENTICAL to pre-N2 — the currently attached session.
        When given, they target that specific ``(agent, session_id)`` instead —
        the read a client uses on a ``session_attached`` switch-barrier to
        rehydrate the NEWLY focused session (which may never have been
        attached in THIS client run) rather than whichever session used to be
        attached. No new ``history.jsonl`` path literal is introduced by this:
        the accessor still resolves through the SAME per-session ``Session``
        object (via the registry's own session-lookup SSoT), just keyed by the
        caller's (agent, session_id) instead of "whichever is attached".

        **Frame-sufficiency boundary.** Like every other session-local read
        (dropdown expansions, the rewind picker), the past-turn log is NOT
        projected onto the AG-UI wire — a REMOTE client holds no session and
        cannot enumerate it. The remote impl therefore degrades gracefully to an
        empty list (never a fabricated turn) REGARDLESS of ``agent``/
        ``session_id`` — remote switch-hydrate is #3310 N3's job, not this
        method's; passing a target here on a remote client is accepted but
        inert."""

    def resolve_conversation_history_source(
        self, *, agent: "str | None" = None, session_id: "str | None" = None,
    ) -> "list[ChatMessage]":
        """#5203: the registry-TOUCHING half of :meth:`conversation_history`
        — MUST be called on the registry's OWNER thread
        (:attr:`~reyn.runtime.registry.AgentRegistry.owner_thread_ident`,
        #4995) by an implementation that genuinely touches one. Returns a
        plain VALUE (never a live ``Session``/registry handle) safe to
        hand across a thread boundary to :meth:`conversation_history_
        from_source`.

        **Concrete, not abstract** — the default delegates straight to
        :meth:`conversation_history` (``limit=None``, the whole log),
        treating its return value AS the "source". This keeps every
        EXISTING :class:`ChatReadModel` implementation (test doubles that
        only ever implemented the single combined method — this is a
        pre-#5203 interface, not a new requirement on them) working
        unchanged: their ``conversation_history`` has no thread-safety
        concern to begin with, so the default split is a no-op split.

        :class:`RegistryReadModel` OVERRIDES this with the REAL split (the
        registry touch, nothing else) — the ONE implementation ``app.py``'s
        #4983 off-loop read (``asyncio.to_thread``) actually calls this
        way, from a genuine second OS thread reaching
        ``AgentRegistry.get_session``/``attached_session`` — #4995 slice 1
        restores the owner-thread guard onto exactly those 2 registry
        methods (the 7-read set #5202 first excluded then #5203
        re-covers) in this SAME PR, so the registry touch must happen on
        the loop, BEFORE the thread hop, not inside it. Every OTHER
        implementation is free to keep using this default — there is
        nothing to hoist when there is no registry to protect."""
        return self.conversation_history(agent=agent, session_id=session_id)

    def conversation_history_from_source(
        self, source: "list[ChatMessage]", *, limit: "int | None" = None,
    ) -> "list[ChatMessage]":
        """#5203: the thread-SAFE half — *source* is whatever
        :meth:`resolve_conversation_history_source` returned. **Concrete,
        not abstract** — the default just applies ``limit`` to *source*
        (already the full resolved log, from the default resolve above);
        see that method's own docstring for why this default is safe for
        every implementation that does not override the split.
        :class:`RegistryReadModel` overrides this too, so the ``limit``
        slice it does here matches this default's own shape exactly —
        kept as one definition, not duplicated, by inheriting it."""
        if limit is not None and limit >= 0:
            return list(source)[-limit:]
        return list(source)

    @abstractmethod
    def load_older_conversation_history(
        self,
        *,
        agent: "str | None" = None,
        session_id: "str | None" = None,
    ) -> int:
        """#4387 Phase B ② (remaining consumers): extend the ON-DISK-backed
        prefix of :meth:`conversation_history` further into the past, for a
        caller that has exhausted what it already paged in and needs to know
        whether more exists — the Textual app's scrollback-top paging
        (:meth:`~reyn.interfaces.inline.textual_chat.app.ChatApp.on_flow_view_reached_top`)
        and search-open (:meth:`~reyn.interfaces.inline.textual_chat.app.ChatApp._materialise_all_older`).

        Unlike ``conversation_history``, which only slices what is ALREADY
        loaded, this actually reads more of ``history.jsonl`` (via
        :meth:`~reyn.runtime.session.Session.extend_history_backward`) —
        because #4387 Phase B ① bounded ``Session.load_history()``'s
        startup read to a tail, a caller that wants to page further back
        than that tail needs a way to ask for more, not just re-slice the
        same bounded list. Returns the count of NEWLY available turns (0 =
        the true start of the conversation was reached — the caller's ONLY
        way to distinguish that from "just haven't paged that far yet").
        The caller then re-reads ``conversation_history()`` for the full
        (now-longer) list.

        **Frame-sufficiency boundary**, same as ``conversation_history``: a
        remote client holds no session and no on-disk history to extend
        into — the remote impl always returns 0 (already-exhausted, never a
        fabricated count)."""

    def completion_source(self) -> "CompletionSourceSnapshot | None":
        """The TUI completion popup's (#3354) sources, as a
        :class:`CompletionSourceSnapshot` VALUE — never the live ``Session``
        (#5044, architect ruling, issuecomment-5378399712/5378442342: see
        that class's own docstring for the full reasoning) — or ``None``
        when this client holds no session at all.

        Two consumers, both session-local by nature: a slash command's
        ``CompleterFn`` is declared as ``(source, arg_partial="") ->
        list[str]`` (``/attach`` reads :attr:`CompletionSourceSnapshot.
        agent_names`, ``/answer`` :attr:`~CompletionSourceSnapshot.
        active_intervention_ids`), and the ``:`` skill completer reads
        :attr:`~CompletionSourceSnapshot.available_skills` — the same list
        the invocation path enforces its visibility surface from.

        **Concrete, not ``@abstractmethod``, unlike every accessor above.** The
        class docstring's "abstract so a partial implementation fails at
        construction" rule exists to catch an implementation that FORGOT a
        member. Here there is nothing to forget: a client that is not the local,
        registry-backed one definitionally holds no session, so ``None`` is the
        complete and correct answer rather than a placeholder — the same
        graceful-degrade the frame-sufficiency boundary above describes, just
        expressed as a default instead of a repeated stub. Every consumer
        already treats a ``None`` source as "offer nothing" (each registered
        ``CompleterFn`` returns ``[]`` for it)."""
        return None


class RegistryReadModel(ChatReadModel):
    """LOCAL read-model — delegates to the same registry/session accessors the
    inline driver called inline before P3 (behavior byte-identical)."""

    def __init__(self, registry, *, agent_name: "str | None" = None) -> None:
        self._registry = registry
        #: #4824: the caller's INTENDED target agent, known before attach can
        #: possibly succeed (same reasoning ``run_repl``'s own docstring
        #: already gives for why it no longer waits on attach). Used ONLY as
        #: :attr:`history_path`'s fallback during the startup race window —
        #: once the initial (or any later ``/attach``) succeeds, ``_attached()``
        #: is never ``None`` again for the rest of this read-model's life, so a
        #: later agent switch never makes this stale value observably wrong.
        self._agent_name = agent_name

    @property
    def capabilities(self) -> ChatReadModelCapabilities:
        return LOCAL_CHAT_READ_CAPABILITIES

    def snapshot(self, config=None):
        return _snapshot(self._registry, config)

    def _attached(self):
        return self._registry.attached_session()

    def completion_source(self) -> "CompletionSourceSnapshot | None":
        """Builds :class:`CompletionSourceSnapshot` synchronously, on
        demand, off the live ``Session`` this read-model already holds —
        always current (no cross-thread delay in this implementation, the
        one production caller as of #5044). See that class's own
        docstring for what each field is and which command reads it."""
        s = self._attached()
        if s is None:
            return None
        return completion_source_snapshot_from_session(s)

    def intervention_head(self):
        s = self._attached()
        return s.interventions.head() if s is not None else None

    def pending_command_ui(self):
        s = self._attached()
        return s.pending_command_ui if s is not None else None

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self) -> Path:
        """#4824: tolerates an unattached registry, same as every OTHER
        seam this class's own module docstring claims already does — a
        piped/non-TTY ``reyn chat`` invocation reaches this property with
        essentially no yield point since ``attach()`` was scheduled (see
        ``run_repl``'s own docstring), so it was NOT a slow-WAL-restore
        race: the background attach task had not ticked even once.

        Falls back to :meth:`AgentRegistry.agent_workspace_dir` (the target
        agent name this read-model was told about at construction, BEFORE
        attach could possibly have succeeded) rather than raising. This is
        the SAME path an attached session's own ``workspace_dir`` would
        report once attach completes (see that method's own docstring for
        why) — not a temporary/in-memory stand-in that gets silently
        replaced later, which would create two truths about where the
        input-history file lives. Once ``_attached()`` stops returning
        ``None`` (initial attach, or any later ``/attach``), this fallback
        is never reached again for the rest of this read-model's life."""
        s = self._attached()
        if s is not None:
            return s.workspace_dir / ".input_history"
        if self._agent_name is not None:
            return self._registry.agent_workspace_dir(self._agent_name) / ".input_history"
        raise RuntimeError(
            "RegistryReadModel.history_path: no attached session and no "
            "target agent_name was given at construction — cannot resolve "
            "a workspace-independent path either"
        )

    def conversation_history(
        self,
        *,
        limit: "int | None" = None,
        agent: "str | None" = None,
        session_id: "str | None" = None,
    ) -> "list[ChatMessage]":
        # #5203: the synchronous do-both convenience — a caller that does
        # not cross a thread boundary (this is most callers) has no reason
        # to split the 2 halves itself. Byte-identical to this method's own
        # pre-#5203 single-piece body; the ONLY caller that DOES cross a
        # thread boundary (``app.py``'s #4983 off-loop read) calls the 2
        # halves directly instead — see ``resolve_conversation_history_
        # source``'s own docstring immediately below.
        source = self.resolve_conversation_history_source(agent=agent, session_id=session_id)
        return self.conversation_history_from_source(source, limit=limit)

    def resolve_conversation_history_source(
        self, *, agent: "str | None" = None, session_id: "str | None" = None,
    ) -> "list[ChatMessage]":
        # #5203: the ONLY registry-touching half of conversation_history's
        # own former single-piece body — split out so a caller crossing a
        # thread boundary (``asyncio.to_thread``, ``app.py``'s #4983 off-
        # loop read) can call THIS half on the registry's OWNER thread and
        # hand the plain VALUE this returns across to the worker thread,
        # instead of letting the worker thread call back into
        # ``AgentRegistry.get_session``/``attached_session`` itself (#4995's
        # own owner-thread invariant, restored onto exactly these 2 methods
        # in the SAME PR — see ``registry.py``'s own ``_assert_owner_thread``
        # call sites). The returned list IS the plain value: a shallow copy
        # of the resolved session's ``history``, no live ``Session``/
        # registry reference retained past this call.
        #
        # #3310 N2: ``agent`` given → resolve THAT session (not necessarily the
        # attached one) via ``AgentRegistry.get_session`` — the same
        # non-loading session-store accessor every other registry call site
        # uses (FP-0043 Stage 3), never a duplicated ``history.jsonl`` path
        # literal. By the time a client processes the ``session_attached``
        # barrier the target session is already loaded (``attach``/
        # ``attach_session`` both ``get_or_load``/require-existing BEFORE
        # announcing), so this is never a cold miss for a switch-driven call.
        if agent is not None:
            s = (
                self._registry.get_session(agent, session_id)
                if session_id is not None
                else self._registry.get_session(agent)
            )
        else:
            s = self._attached()
        if s is None:
            return []
        # The Session's ``history`` list IS the ChatMessage log loaded from
        # ``history.jsonl`` by ``Session.load_history`` at load time — read it
        # straight (no audit-event path). ``list(...)`` is a shallow copy: the
        # LIST is a new object, so neither this caller nor a later
        # ``conversation_history_from_source`` slice/append can add or drop
        # entries in the live session history. The ELEMENTS (``ChatMessage``
        # instances) are the SAME objects, shared with the live list — an
        # in-place mutation of one WOULD reach the live session. Nothing on
        # this path does that today (docs-maintainer's #5215 B confirmed), so
        # this is a documentation-accuracy note, not a live hazard.
        return list(getattr(s, "history", []) or [])

    def conversation_history_from_source(
        self, source: "list[ChatMessage]", *, limit: "int | None" = None,
    ) -> "list[ChatMessage]":
        # #5203: the thread-safe half — *source* is already a plain value
        # (whatever :meth:`resolve_conversation_history_source` returned),
        # never a live ``Session``/registry handle, so this method touches
        # neither ``self._registry`` nor any ``Session`` attribute — safe
        # to call from ANY thread, including inside ``asyncio.to_thread``.
        if limit is not None and limit >= 0:
            return source[-limit:]
        return list(source)

    def load_older_conversation_history(
        self,
        *,
        agent: "str | None" = None,
        session_id: "str | None" = None,
    ) -> int:
        # Same target-resolution shape as ``conversation_history`` above —
        # the currently attached session unless a specific (agent,
        # session_id) is named.
        if agent is not None:
            s = (
                self._registry.get_session(agent, session_id)
                if session_id is not None
                else self._registry.get_session(agent)
            )
        else:
            s = self._attached()
        if s is None:
            return 0
        extend = getattr(s, "extend_history_backward", None)
        if extend is None:
            return 0
        return extend()


#: #5093 (architect ruling, issuecomment-5384873023): the ``STATE_*`` wire
#: keys that ``project_remote_snapshot`` reads via a ``v.get(key, <placeholder>)``
#: call where the placeholder is genuinely unreachable — these keys have NO
#: "unsupported"/"not yet on the wire" state to declare (unlike
#: ``agent_names``/``model_classes``/etc, which DO — see
#: ``ChatReadModelCapabilities.agent_roster_reported``/``model_catalog_reported``
#: — and unlike a bare non-``.get`` literal like ``cron_jobs: []``, which
#: needs its own declared axis). ``scripts/check_remote_snapshot_placeholder_
#: declared.py`` imports this SAME constant rather than re-typing the key
#: list — architect's explicit condition on approving the exclusion at all:
#: "5件をgate側に書き写さない... 将来keyがwireから外れたとき、gateは除外した
#: ままで穴が開く" (same comment as above, issuecomment-5384873023) — a key
#: removed from here automatically re-enters the gate's scope, no second
#: edit required.
#:
#: ``cost_usd`` is NOT listed separately: its own ``.get(...)`` call reads
#: the ``"cost_agent"`` wire key (an intentional alias, see the dict below),
#: so the gate matches on the ``.get()`` call's own key ARGUMENT, not the
#: dict's output key name — one membership check covers both.
#:
#: ``queue``/``turn_active``/``queue_seq`` (#3300 P2b/#5098) are the
#: server-authoritative sent-queue state, folded onto the wire the same way
#: the cost/ctx figures are — see ``project_remote_snapshot``'s own inline
#: comment at those 3 keys for the citation.
_WIRE_KEYS = frozenset({
    "cost_agent", "cost_total", "agent_tokens", "ctx_used", "ctx_window",
    "queue", "turn_active", "queue_seq",
})


def project_remote_snapshot(values: "dict | None") -> dict:
    """Project a :class:`RemoteStatusView`'s wire values into the ``_snapshot``
    dict shape the inline chips read.

    The MAIN-bar keys are filled from the wire; every EXPANSION-only key gets a
    graceful empty/zero default so opening a dropdown on a remote client shows an
    empty panel rather than raising or fabricating a value. ``model`` falls back
    to ``—`` so a pre-``STATE_SNAPSHOT`` frame renders a placeholder, not None.
    """
    v = values or {}
    return {
        # -- MAIN bar (frame-available via STATE_*) --
        "model": v.get("model") or "—",
        "attached_name": v.get("attached_name"),
        # #5094: real wire data (agui/state.py's own ``project_status`` dict
        # carries these 4, #5098) — previously a hand-typed `[]`/`None` literal here
        # UNCONDITIONALLY emptied a remote client's agent tab and
        # model-class picker regardless of how many agents/model classes
        # the server actually had (owner live-blocked on this, #5041).
        # `agent_names`/`session_tree` are literally what
        # RegistryReadModel's own `agent_names`/`session_tree` methods
        # supply locally (`registry.loaded_names()`/`registry.
        # session_tree()`); `model_active_class`/`model_classes` mirror
        # `Session.active_model_class()`/`known_model_classes()` the
        # same way `model` above already does.
        "model_active_class": v.get("model_active_class"),
        "model_classes": v.get("model_classes", []),
        "agent_names": v.get("agent_names", []),
        "session_tree": v.get("session_tree", []),
        "cost_agent": v.get("cost_agent", 0.0),
        "cost_total": v.get("cost_total", 0.0),
        "cost_usd": v.get("cost_agent", 0.0),
        "agent_tokens": v.get("agent_tokens", 0),
        "ctx_used": v.get("ctx_used", 0),
        "ctx_window": v.get("ctx_window", 0),
        # -- session-local keys (NOT on the wire) → graceful empty/zero --
        #
        # #5009 / #5009 closing pass: every ``*_reported`` declaration
        # (whether the VALUE next to it here can be trusted, or is a
        # graceful degrade with no reader-visible way to tell) is
        # projected in ONE call, from ONE source
        # (``REMOTE_CHAT_READ_CAPABILITIES``) — never hand-typed per key.
        # A hand-typed literal per key was tried and measured wrong (a
        # producer that forgets one key can silently claim "I report"
        # while returning a fabricated value); one generic projection
        # over ``ChatReadModelCapabilities``'s own fields (see
        # :func:`reported_snapshot_keys`) removes the "forgot to wire
        # this key" failure mode structurally — a new field declared on
        # that dataclass reaches every producer for free, no call-site
        # edit required.
        **reported_snapshot_keys(REMOTE_CHAT_READ_CAPABILITIES),
        # The prompt/completion SPLIT below is `0`/`0` while the total is
        # real wire data — an inconsistent breakdown (`0 + 0 != total`)
        # the Cost pane never flags on its own; gated by
        # ``usage_breakdown_reported`` above.
        "usage": (0, 0, v.get("agent_tokens", 0)),
        # `0`/`(0, 0)` below are the correct graceful-degrade VALUES for
        # both cache figures (neither is projected onto the AG-UI wire —
        # cache-hit accounting is session-local); gated by
        # ``cache_usage_reported`` above, consulted by `chrome.py`'s
        # `_cache_hit_line` so BOTH panes reading these 2 keys (Cost
        # pane's cumulative line, Ctx pane's recent-call line) render a
        # "not reported" line instead of a fabricated "0% hit (0 / 0)".
        #
        # Scope, explicit (architect, #5009): this is NOT the owner's actual
        # "cache stuck at 0%" observation — that was measured on a LOCAL
        # session (owner-confirmed) and is a separate, still-unresolved
        # symptom this key does not touch. This key only makes the REMOTE
        # "0 could mean unsupported" case honestly say so.
        "session_cached_tokens": 0,
        "ctx_recent_usage": (0, 0),
        "ctx_source": "remote",
        # `None` below is correct (no compaction-status source on the
        # wire); gated by ``ctx_compaction_reported`` above so the Ctx
        # pane renders "not reported" instead of the fabricated-looking
        # "0 / 0 tokens est. (0% to trigger)" a naive ``status_fn is
        # None`` fallback produces (a real local session essentially
        # never has a genuine zero-trigger state, so "0%" here reads as
        # false reassurance, not as an honest empty state).
        "ctx_compaction_status_fn": None,
        # #3283 ④: the keyed per-turn cost/token lookup is a SESSION-local read
        # (the tracker's per-turn buckets are process-local, in-memory, and not
        # projected onto the AG-UI wire) → None for remote, and the right
        # gutter renders "—" rather than a fabricated figure. Same
        # frame-sufficiency boundary as ``conversation_history`` above.
        "turn_usage_fn": None,
        # `[]` below is correct (cron config is not on the wire); gated
        # by ``cron_jobs_reported`` above so the Cron pane renders "not
        # reported" instead of its own `["(none)"]` fallback, which is
        # byte-identical to a genuinely empty LOCAL cron config.
        "cron_jobs": [],
        "mcp_servers": [],
        # `[]` below is correct (hook config is not on the wire); gated by
        # ``hooks_reported`` above so `_hook_pane_entries` renders "not
        # reported" instead of its own `["(none)"]` fallback (#5034 — same
        # fabrication ``cron_jobs_reported`` already closed for the Cron
        # pane, missed by #5009's original hand-enumerated key list).
        "hooks": [],
        "skills": [],
        # #5185 (reverses #3378/#4686's own hand-typed literals below —
        # see the ChatReadModelCapabilities docstring's own section):
        # both keys now ride the wire for real
        # (agui/state.py's own project_status, same channel #5094 used for
        # agent_names etc). ``visibility_items`` is read straight off the
        # wire, PRESERVING None — a bare `v.get(...)` with NO `[]` default,
        # deliberately: defaulting to `[]` here would be the exact
        # fabrication #3378 first existed to prevent, just moved from a
        # permanent hand literal to a wire-miss default. `None` still means
        # "this session wires no visibility seam" (`_visibility_pane_rows`'s
        # own `raw is not None` check, unchanged) — now genuinely reflecting
        # the SERVER's real per-session state instead of an unconditional
        # remote-side constant.
        "visibility_items": v.get("visibility_items"),
        # gated by ``hooks_reported`` alongside ``hooks`` above — one pane,
        # one field (#5034).
        "hook_items": [],
        # #5185: real wire data — `_session_mcp_subscriptions` always
        # returns a list (never None, unlike `visibility_items` above; see
        # its own docstring, "not a seam like visibility_items/hook_items"),
        # so `[]` is the correct default for BOTH "genuinely no
        # subscriptions" and "key not yet on the wire" here.
        "mcp_subscriptions": v.get("mcp_subscriptions", []),
        # `[]` below is correct (pipeline registry is not on the wire);
        # gated by ``pipelines_reported`` above so `pipe_pane_lines`
        # renders "not reported" instead of its own `["(none)"]` fallback
        # (#5034 — same fabrication shape as ``hooks_reported`` above).
        "pipelines": [],
        # #3300 P2b: the server-authoritative sent-queue state IS on the wire
        # (state.py's ``project_status``, folded in by P2a, #5098) —
        # project it through unlike the session-local keys above, so
        # ``ChatReadModel.snapshot()`` returns queue info uniformly for BOTH
        # local (``RegistryReadModel``, straight off ``_snapshot()``) and
        # remote clients (local ≡ remote by construction, the read-model's own
        # governing rule — see the module docstring). This is what lets the
        # Textual sent-queue widget seed its ``RemoteQueueView`` baseline from
        # ONE seam (``read_model.snapshot()``) regardless of transport.
        "queue": v.get("queue", []),
        "turn_active": v.get("turn_active", False),
        "queue_seq": v.get("queue_seq", 0),
        # #4194: session-local (the ReynConfig each side loaded from ITS OWN
        # reyn.yaml — not projected onto the wire, same frame-sufficiency
        # boundary as model_classes/agent_names above). A remote client's
        # policy-tier config is the SERVER's, not the client's, so there is
        # nothing meaningful to report here; 0 degrades gracefully (no
        # indicator shown) rather than fabricating a count.
        "unknown_config_key_count": 0,
        # #4357: same reasoning — an empty dict degrades gracefully to the
        # count-only (here, no-indicator-at-all) fallback in
        # config_warning_text, never a fabricated key list.
        "unknown_config_keys": {},
    }


class RemoteReadModel(ChatReadModel):
    """REMOTE read-model — projects the transport's P2 ``RemoteStatusView`` (fed by
    the server's ``STATE_SNAPSHOT`` / ``STATE_DELTA``) into the status-bar shape.

    Region/command-UI are inline-app-local and not on the wire → empty. The
    input-history file lands under ``~/.reyn`` (there is no per-agent workspace on
    the client side of a remote attach)."""

    def __init__(self, transport: "ClientTransport") -> None:
        self._transport = transport

    @property
    def capabilities(self) -> ChatReadModelCapabilities:
        return REMOTE_CHAT_READ_CAPABILITIES

    def snapshot(self, config=None):
        # ``transport.status`` is the RemoteStatusView the AgUiTransport updates as
        # it decodes STATE_* frames; read it live each render tick.
        values = getattr(getattr(self._transport, "status", None), "values", None)
        return project_remote_snapshot(values)

    def completion_source(self):
        # Frame-sufficiency, same boundary as every other session-local read:
        # a remote client holds no Session, and neither the agent registry a
        # ``CompleterFn`` walks nor the skill entry list is projected onto the
        # wire. Stated explicitly here rather than inherited so the remote
        # DECISION is visible at the remote implementation (the base default
        # happens to agree). ``/`` command-name completion is unaffected — it
        # comes off the process-local slash registry, not the session.
        return None

    def intervention_head(self):
        # #5050: previously unconditional None — see this module's #5050
        # commit for the full account. STATE_SNAPSHOT/STATE_DELTA now carry
        # ``pending_intervention_head`` (status._snapshot's own doc), so a
        # remote client CAN read it, the same live-tick pattern
        # :meth:`snapshot` above already uses (``transport.status.values``).
        # Returns the JSON-safe dict projection ({id, prompt, detail,
        # choices: [{id, label, hotkey}, ...]}), not a ``UserIntervention``
        # instance — a remote consumer never held a real one to begin with.
        values = getattr(getattr(self._transport, "status", None), "values", None)
        return (values or {}).get("pending_intervention_head")

    def pending_command_ui(self):
        return None

    @property
    def has_command_ui_region(self) -> bool:
        return False

    @property
    def history_path(self) -> Path:
        base = Path.home() / ".reyn"
        base.mkdir(parents=True, exist_ok=True)
        return base / "remote-input-history"

    def conversation_history(
        self,
        *,
        limit: "int | None" = None,
        agent: "str | None" = None,
        session_id: "str | None" = None,
    ) -> "list[ChatMessage]":
        # #5203: no registry to touch on this side — a remote client holds
        # no session. Frame-sufficiency: the past-turn ChatMessage log is
        # NOT projected onto the wire (like the dropdown expansions /
        # rewind picker). Degrade gracefully to empty — never a fabricated
        # turn, regardless of a targeted (agent, session_id) — remote
        # switch-hydrate is #3310 N3's job, not this accessor's. No
        # override of resolve_conversation_history_source/conversation_
        # history_from_source needed: the base class's own defaults
        # delegate straight to this method and already produce [] → [].
        return []

    def load_older_conversation_history(
        self,
        *,
        agent: "str | None" = None,
        session_id: "str | None" = None,
    ) -> int:
        # Frame-sufficiency: no session, no on-disk history to extend into.
        # 0 reads as "nothing more to load," never a fabricated count — the
        # same graceful-degrade every other session-local accessor here uses.
        return 0


__all__ = [
    "ChatReadModel",
    "ChatReadModelCapabilities",
    "LOCAL_CHAT_READ_CAPABILITIES",
    "REMOTE_CHAT_READ_CAPABILITIES",
    "RegistryReadModel",
    "RemoteReadModel",
    "project_remote_snapshot",
]
