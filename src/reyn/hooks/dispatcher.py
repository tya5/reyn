"""reyn.hooks.dispatcher — the awaited HookDispatcher (#1800 slice 5b).

The integration core of the agent-lifecycle-hook system. Unlike a P6 EventLog
subscriber (sync-inline, cannot ``await``), the dispatcher is a **first-class
``await``ed dispatch** invoked at the session/turn lifecycle points: it can
``await`` the inbox push (E), the next-turn staging (C), and the shell run (F).

Per-hook isolation: a hook that raises is logged and skipped; its siblings and
the lifecycle point itself proceed. ``dispatch()`` never propagates an exception
out — a misbehaving hook can never break the run-loop.

No-hooks equivalence (the critical property): an empty registry makes
``dispatch()`` a no-op (the ``hooks_for`` loop body never runs), so the run-loop
is byte-identical to a hooks-free build.

The four Session seams the dispatcher needs are injected as bound callables
(DI), so the dispatcher is decoupled from ``Session`` and unit-testable against a
real Session's methods (no mocks):

- ``put_inbox(kind, payload)``           — E (wake=true): a turn trigger.
- ``stage_next_turn_context(kind, payload)`` — C (wake=false): a passive ride-along.
- ``run_shell(command, event_context, **sandbox)`` — F: an external side-effect.
- ``launch_pipeline(name, input)``       — #2608 H3: launch a registered
  Pipeline (async/detached — the launched pipeline's result arrives later on
  the session's own inbox as a ``pipeline_result`` message).

#5084 ④ adds two more, same DI/callable posture: ``hook_cwd()`` and
``hook_process_context()`` — read LIVE at each ``exec``/``exec_capture``
dispatch (not frozen at construction), so a relative argv resolves inside
the dispatching agent's OWN tree (``Session._workspace_base_dir``) and the
child process receives the closed ``REYN_*`` envelope
(:class:`~reyn.hooks.shell_runner.HookProcessContext`) for THAT agent.
Before this, every hook exec silently inherited reyn's own launch cwd
regardless of which agent dispatched it — a real gap, not merely
unconfirmed until #5084 ④ measured it.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from reyn.hooks.bus import HookBus
from reyn.hooks.event import HookEvent
from reyn.hooks.event_pattern import from_legacy_matcher
from reyn.hooks.event_pattern import matches as event_pattern_matches
from reyn.hooks.registry import HookRegistry
from reyn.hooks.render import ResolvedPush, render_pipeline_input, render_push
from reyn.hooks.schema import HookDef
from reyn.hooks.schema_registry import canonical_kind
from reyn.hooks.shell_runner import run_shell_hook  # runs exec/exec_capture argv (#3226 P4)
from reyn.runtime.chat_message import Spillability
from reyn.runtime.turn_origin import TurnOrigin

_log = logging.getLogger(__name__)

# The kind stored on a staged C (wake=false) ride-along entry; the staged-context
# consumer reads ``payload["name"]`` for the ``[hook:name]`` attribution.
HOOK_STAGE_KIND = "hook"

PutInbox = Callable[[str, dict], Awaitable[Any]]
StageContext = Callable[[str, dict], Awaitable[Any]]
RunShell = Callable[..., Awaitable[Any]]
# #5516: the ingress bridges' hook_trigger callable now takes a BATCH of
# payload dicts (never a bare single dict — clean break, no dual shape),
# folded upstream by reyn.hooks.fold.drain_folded. See
# HookDispatcher.dispatch_external_batch's own docstring.
HookTriggerBatch = Callable[[str, "list[dict]"], Awaitable[Any]]
# #2608 H3: launch a registered pipeline by name with a rendered input dict
# (or None). Returns whatever the injected callable returns (unused by the
# dispatcher — the launch is fire-and-continue).
LaunchPipeline = Callable[[str, "dict | None"], Awaitable[Any]]


class HookDispatcher:
    """Awaited dispatch of lifecycle hooks (#1800 slice 5b)."""

    def __init__(
        self,
        registry: HookRegistry,
        *,
        put_inbox: PutInbox,
        stage_next_turn_context: StageContext,
        run_shell: RunShell = run_shell_hook,
        # #2608 H3: launch a registered pipeline (async/detached). None (the
        # default — e.g. a unit test that never configures pipeline_launch) →
        # a pipeline_launch hook logs a clear warning and is skipped, same
        # per-hook-isolation posture as every other action.
        launch_pipeline: "LaunchPipeline | None" = None,
        sandbox_config: Any = None,
        sandbox_backend: Any = None,
        consent_bus: Any = None,
        consent_gate: "Callable[[], bool] | None" = None,
        emit_event: "Callable[..., Any] | None" = None,
        cross_session_put: "Callable[..., Any] | None" = None,
        current_session_id: "str | None" = None,
        is_hook_disabled: "Callable[[HookDef], bool] | None" = None,
        bus: "HookBus | None" = None,
        hook_cwd: "Callable[[], str | None] | None" = None,
        hook_process_context: "Callable[[], Any] | None" = None,
        hook_temp_dir: "Callable[[], str | None] | None" = None,
        resolve_exec_capture_output_cap: "Callable[[], tuple[int, str] | None] | None" = None,
    ) -> None:
        self._registry = registry
        # Hook-Event Redesign Phase 4a (proposal 0059 §3.2/§3.3): the optional
        # per-Session Async Bus. None (the default — every pre-Phase-4a call
        # site, including every pre-Phase-4a test) keeps dispatch() byte-
        # identical to before this module existed; when set, dispatch()
        # broadcasts a copy-free HookEvent to it INDEPENDENTLY of the Sync
        # hooks_for() loop below (see dispatch()'s docstring).
        self._bus = bus
        # #2285: per-session hook APPLICABILITY gate — consulted at dispatch time (live) so a hook
        # disabled for THIS session is skipped. Deferred (a callable, not a snapshot) so a toggle
        # applies to the next dispatch without rebuilding the dispatcher. ``None`` → no gate
        # (byte-identical to pre-#2285). Per-session by construction: each session's dispatcher gets
        # its own predicate over its own disabled-set.
        self._is_hook_disabled = is_hook_disabled
        self._put_inbox = put_inbox
        self._stage_next_turn_context = stage_next_turn_context
        # #2072: cross-session push routing. ``cross_session_put(target_sid, kind, payload,
        # wake=...)`` delivers a push to ANOTHER session's inbox (the canonical wake-triple);
        # ``current_session_id`` identifies THIS session so a push naming it (or naming none)
        # stays local. None ``cross_session_put`` (e.g. unit tests / no registry) → the push
        # always stays local — the pre-#2072 behaviour, no-op-equivalent.
        self._cross_session_put = cross_session_put
        self._current_session_id = current_session_id
        self._run_shell = run_shell
        self._launch_pipeline = launch_pipeline
        self._sandbox_config = sandbox_config
        self._sandbox_backend = sandbox_backend
        # #2095 P3: P6-event sink for shell-hook executions, so an auto-run
        # (allowlisted) shell hook surfaces in the events tab instead of being a
        # silent side-effect. None → no emission (e.g. unit tests).
        self._emit_event = emit_event
        # #2095: the session RequestBus + a LIVE "is a listener attached?" gate,
        # forwarded to the shell-hook consent gate so a not-yet-allowlisted
        # command's prompt surfaces on the answering surface (TUI Pending tab)
        # rather than the stdin prompt. ``_consent_bus_now()`` returns the bus
        # ONLY when ``consent_gate()`` is true at dispatch time (a listener is
        # registered — TUI/web/A2A-override); otherwise None, so the runner
        # takes its stdin / fail-closed path (plain mcp-serve, headless, and
        # ``reyn run`` with no listener all hit this). Evaluated per-dispatch
        # because listeners attach/detach after construction (TUI mount, A2A
        # request windows).
        self._consent_bus = consent_bus
        self._consent_gate = consent_gate
        # #5084 ④: cwd/hook_process_context for exec/exec_capture argv, sourced
        # as CALLABLES (not values frozen at construction time) — same idiom
        # as is_hook_disabled/consent_gate above, because the agent's own
        # workspace base_dir is LIVE (Session._workspace_base_dir can change
        # across this dispatcher's lifetime; #5081). None (the default —
        # every pre-#5084 call site) → no cwd/env addition, byte-identical to
        # before this parameter existed (hook exec inherits reyn's own launch
        # cwd, same as always).
        self._hook_cwd = hook_cwd
        self._hook_process_context = hook_process_context
        self._hook_temp_dir = hook_temp_dir
        # #5210: same "live callable, not a value frozen at construction time"
        # idiom as hook_cwd/hook_process_context above — the context budget
        # a live Session/TurnBudgetEngine derives can change across this
        # dispatcher's lifetime (a model switch, e.g.). None (the default —
        # every pre-#5210 call site) → no cap applied, byte-identical to
        # before this parameter existed (exec_capture's returned stdout is
        # unbounded, same as always — see shell_runner.run_shell_hook's own
        # docstring for why #5210 does not invent a fallback number here).
        self._resolve_exec_capture_output_cap = resolve_exec_capture_output_cap

    @property
    def registry(self) -> HookRegistry:
        """The currently-live :class:`HookRegistry` (#5167). Read-only
        introspection for a caller that needs to enumerate declared hooks
        BEFORE any dispatch happens (e.g. ``Session``'s own session-start
        auto-subscribe pass over declared ``mcp_resource_updated`` hooks) —
        never reach into ``self._registry`` directly. Reflects
        :meth:`replace_registry`'s live swap, same as ``dispatch()`` itself."""
        return self._registry

    def _consent_bus_now(self) -> Any:
        """The consent bus iff a live intervention listener is attached, else None.

        #5536 (architect ruling — group B, "挙動が変わり、かつ silent"):
        ``self._consent_gate()`` raising is not a hypothetical — it is a
        caller-supplied callable, live-evaluated per-dispatch (see this
        class's own ``__init__`` docstring for why it is never frozen).
        Before this fix, a raise here returned ``None`` with NO log —
        the shell-hook consent path then falls back to its own
        stdin/fail-closed branch (never fail-OPEN; that path itself is
        safe), but in an unattended/non-TTY session "ask the operator"
        silently becomes "refuse", and nothing records WHY. ``_log.
        warning`` (never an audit-event — auditing a gate-evaluation
        failure through the SAME consent machinery it is about to bypass
        would be its own quieter failure mode) so the fallback and its
        cause are at least visible in the process log, even though the
        fallback itself was already the safe direction."""
        if self._consent_bus is None or self._consent_gate is None:
            return None
        try:
            return self._consent_bus if self._consent_gate() else None
        except Exception as exc:  # noqa: BLE001 — a gate error must not break dispatch
            _log.warning(
                "Hook consent gate raised — falling back to the fail-closed "
                "stdin/non-TTY consent path (never fail-open) for this "
                "dispatch: %s: %s", type(exc).__name__, exc,
            )
            return None

    def replace_registry(self, registry: HookRegistry) -> None:
        """Swap the live hook registry (#2073 S2b config hot-reload). ``dispatch()``
        reads ``self._registry`` fresh on every lifecycle point, so a single swap
        here propagates to every holder of this dispatcher instance — no re-threading
        through the kernel/router seams. Used by the Session's hooks reapply seam to
        install ``startup ∪ re-read-runtime`` hooks at the turn boundary."""
        self._registry = registry

    async def dispatch(self, point: str, template_vars: dict) -> None:
        """Run every hook registered for ``point`` (registration order).

        Per-hook ``try/except``: a raising hook is logged + skipped; siblings and
        the lifecycle point proceed. Never propagates out of ``dispatch()``.
        Empty registry → the loop body never runs → byte-identical no-op.

        #2608 H2: before running a hook's action, its (optional) ``matcher`` is
        evaluated against ``template_vars`` (``reyn.hooks.matcher.matches``) — a
        non-matching hook is skipped, same as a disabled hook. A hook with no
        matcher always matches (fire-always, unchanged from pre-H2).

        Hook-Event Redesign Phase 3 (proposal 0059 §10 Q-reyn-4): the matcher
        check below evaluates through the generalized ``EventPattern`` grammar
        (``reyn.hooks.event_pattern``) rather than calling
        ``reyn.hooks.matcher.matches`` directly — ``hook.matcher`` is wrapped
        into a payload-only ``EventPattern`` (``from_legacy_matcher``), whose
        ``kind``/``source`` predicates are unset, so evaluation is
        byte-identical to the pre-Phase-3 direct call (the payload predicate
        itself still delegates to ``reyn.hooks.matcher.matches`` — UNCHANGED).

        Hook-Event Redesign Phase 1 (proposal 0059 §1): ``point`` +
        ``template_vars`` are wrapped into a typed ``HookEvent`` right here —
        the SAME dict object becomes ``HookEvent.payload`` (no copy, no value
        change), so every existing call site's external shape (``dispatch(point,
        template_vars)``) is untouched and behavior stays byte-identical.
        ``dispatch()`` deliberately does NOT schema-validate the payload here
        (that happens at the PRODUCER side, ``schema_registry.
        build_hook_payload`` — see that module's docstring for why): a hook may
        legitimately be dispatched with an arbitrary/partial dict (tests, and
        any future non-builtin point), and this per-hook-isolation boundary is
        about a HOOK's action failing, not about producer-schema drift.

        Hook-Event Redesign Phase 4a (proposal 0059 §3.2): immediately after
        constructing ``event``, it is broadcast to this dispatcher's (optional)
        ``HookBus`` — UNCONDITIONALLY, before the Sync ``hooks_for()`` loop
        below runs and regardless of whether that loop finds any registered
        hook for ``point``. This is what makes Sync and Bus independent (§3.2):
        a Bus-only subscriber observes every dispatched event even with zero
        Sync hooks configured for ``point``, and a Sync hook's execution below
        is entirely unaffected by whether any Bus subscriber exists. ``bus is
        None`` (the default) skips this line — the no-bus happy path stays
        byte-identical to pre-Phase-4a.
        """
        await self._dispatch_batch_for_point(
            point, [self._wrap_lifecycle_event(point, template_vars)],
            republish_to_bus=True, skipped_session_wide=0,
        )

    def _wrap_lifecycle_event(self, point: str, template_vars: dict) -> HookEvent:
        return HookEvent(
            kind=canonical_kind(point), payload=template_vars,
            chain_id=template_vars.get("chain_id"),
        )

    async def dispatch_external_batch(
        self, point: str, payloads: "list[dict]", *, skipped_session_wide: int = 0,
    ) -> None:
        """#5516 — the batched entry point an ingress bridge's
        ``hook_trigger`` is bound to (``_BoundedEventBridge``'s ``deliver``/
        ``drain_folded``-driven drain, for ``mcp_resource_updated`` /
        ``file_changed``). Replaces the old ``dispatch(point, template_vars)``
        binding at the SAME injection site (``runtime/session.py``'s
        ``hook_trigger=`` lambdas) — ``dispatch()`` itself is now used ONLY
        by the six lifecycle call sites (which never batch — each fires
        once per turn boundary, so their own event just rides through here
        as a length-1 batch, see ``_wrap_lifecycle_event``).

        *payloads* is the FOLDED batch ``reyn.hooks.fold.drain_folded``
        assembled (never empty; owner ruling #5516 §2: N events -> 1
        launch carrying N items, never N events -> 1 event). Each payload
        becomes its own :class:`HookEvent`, independently published to the
        Bus (§3.2's "every dispatched event observed independently" stays
        true even when folded — folding is a SYNC-dispatch launch-count
        optimization, not a Bus semantics change).

        *skipped_session_wide* — #5516 §2: the count of events LOST to
        bridge queue overflow since the last dispatch from THIS bridge
        (before any per-hook matcher ever saw them, hence "session_wide",
        never attributable to one hook entry — see
        ``_dispatch_batch_for_point``'s own docstring for why the field
        name must not claim an attribution it cannot make)."""
        events = [self._wrap_lifecycle_event(point, p) for p in payloads]
        await self._dispatch_batch_for_point(
            point, events, republish_to_bus=True,
            skipped_session_wide=skipped_session_wide,
        )

    async def dispatch_bus_event(self, event: HookEvent) -> None:
        """Single-event convenience wrapper over
        :meth:`dispatch_bus_event_batch` (``[event]``) — kept for any
        caller that has exactly one event and no folding to do. See that
        method's docstring for the full contract."""
        await self.dispatch_bus_event_batch([event])

    async def dispatch_bus_event_batch(self, events: "list[HookEvent]") -> None:
        """#5516 — the batched sibling of ``dispatch_bus_event``: run every
        Sync-registered hook whose ``on:`` equals ``events[0].kind`` (the
        caller — ``reyn.hooks.composed_consumer.ComposedEventConsumer`` —
        groups a raw ``drain_folded`` batch by kind BEFORE calling this,
        since its own subscription queue is NOT single-kind like a bridge's
        — see that module for the grouping step), for events that arrived
        via the Bus rather than through ``dispatch()``'s own lifecycle call
        sites.

        Unlike ``dispatch_external_batch``, this method does NOT
        re-publish to ``self._bus`` — every ``event`` here already arrived
        via the bus, so re-broadcasting would be a duplicate delivery to
        any sibling Composer/subscriber correlating on the same kind.
        Per-hook isolation and the matcher/applicability gates are
        otherwise identical.

        A ``template_push`` hook's wake=true action lands in the inbox via
        the SAME ``_push_resolved`` E-path (``TurnOrigin.HOOK``/kind="hook")
        every other hook-driven wake uses, so folding N composed events into
        ONE launch reduces N inbox turns to 1 directly (owner ruling #5516
        §1/③) — this is now the primary way to bound a hook-driven turn
        burst, #5561 having retired the ``max_hook_driven_turns`` valve
        this comment used to cite here."""
        if not events:
            return
        point = events[0].kind
        # skipped_session_wide for the composed path is threaded by the
        # caller (ComposedEventConsumer reads its own HookBus drop-count
        # delta) — this method receives an already-grouped, same-kind
        # batch and has no queue of its own to read from, so it always
        # passes 0 here; ComposedEventConsumer folds its own skip count
        # into event_context by calling dispatch_external_batch-shaped
        # bookkeeping itself (see that module).
        await self._dispatch_batch_for_point(
            point, events, republish_to_bus=False, skipped_session_wide=0,
        )

    async def _dispatch_batch_for_point(
        self, point: str, events: "list[HookEvent]", *,
        republish_to_bus: bool, skipped_session_wide: int,
    ) -> None:
        """#5516 — the shared core both ``dispatch()``/
        ``dispatch_external_batch`` and ``dispatch_bus_event_batch``
        delegate to (one implementation, not two independently-drifting
        copies — CLAUDE.md). ``events`` is the RAW, matcher-blind batch;
        the fold unit is **(session, hook entry)**, not (session, point):
        each hook in ``hooks_for(point)`` gets its OWN subset of ``events``
        — only the ones whose ``template_vars`` match THAT hook's own
        ``matcher`` (#5516 §2 — two hooks on the same point can have
        different matchers, e.g. two ``mcp_resource_updated`` hooks scoped
        to different ``uri`` globs; folding must happen AFTER that
        per-hook filter, never before, or one hook's batch would silently
        include events meant only for its sibling).

        ``skipped_session_wide`` — by contrast — is a SESSION-scoped count
        (#5516 §2): it happens BEFORE any matcher ever ran (queue overflow
        at the bridge, upstream of this method entirely), so it cannot be
        attributed to any one hook entry — the SAME number is threaded
        into every hook's ``event_context`` for this batch, with a field
        name that says so (``skipped_session_wide``, never a bare
        ``skipped`` that would read as "your entries were skipped").

        #5516 §1/§1b: ``hook.fold is False`` (the operator's explicit
        opt-out) makes this hook receive its matched events as N
        SEPARATE single-item-array launches instead of one N-item
        launch — the array-wrapping itself is unconditional either way
        (clean break), only the LAUNCH COUNT differs. ``skipped_session_
        wide`` still rides along (owner §1b item ②: it applies
        regardless of this flag), but only on the FIRST of the N calls
        — the count is session-wide, not per-event, so reporting it N
        times would over-count the same drop."""
        if republish_to_bus and self._bus is not None:
            for event in events:
                self._bus.publish(event)
        for hook in self._registry.hooks_for(point):
            if self._is_hook_disabled is not None and self._is_hook_disabled(hook):
                continue  # #2285: hook disabled for THIS session (live applicability toggle)
            matched = [
                event for event in events
                if event_pattern_matches(from_legacy_matcher(hook.matcher), event)
            ]
            if not matched:
                continue  # #2608 H2: matcher didn't match any event's template_vars
            try:
                if hook.fold is False:
                    # #5516 opt-out: one launch per event, not one launch
                    # for the whole matched batch.
                    for i, event in enumerate(matched):
                        await self._dispatch_one_batch(
                            hook, point, [event],
                            skipped_session_wide=(skipped_session_wide if i == 0 else 0),
                        )
                else:
                    await self._dispatch_one_batch(
                        hook, point, matched, skipped_session_wide=skipped_session_wide,
                    )
            except Exception as exc:  # noqa: BLE001 — per-hook isolation boundary
                _log.warning(
                    "Hook at point %r raised — skipped (siblings proceed). "
                    "hook=%r error=%s: %s",
                    point, hook, type(exc).__name__, exc,
                )

    async def _dispatch_one_batch(
        self, hook: HookDef, point: str, events: "list[HookEvent]", *,
        skipped_session_wide: int,
    ) -> None:
        """Dispatch ONE hook against a (session, hook-entry)-scoped batch
        of ``events`` (#5516 — never empty; the caller already filtered to
        this hook's own matching subset). Scheme-by-scheme, per action:

        - ``exec``/``exec_capture``: the subprocess's stdin JSON
          (``event_context``) can carry N items in one call — always an
          array (clean break, #5516 §1: N=1 too, ``[payload]``), wrapped
          with ``skipped_session_wide`` (never attributable to this one
          hook — see ``_dispatch_batch_for_point``'s docstring).
        - ``template_push``: renders once PER event, then concatenates
          the N resolved messages into ONE push (owner ruling #5516 §1
          item ③ — reduces N inbox turns to 1 directly, now the primary
          way to bound a hook-driven turn burst since #5561 retired the
          count-capping loop valve this comment used to cite here).
        - ``pipeline_launch``: does **NOT fold** — architect ruling
          (#5516 broker thread, 2026-08-29): the discriminator for
          whether an action CAN fold is not "does it render" but "can the
          receiver take N items in ONE call": exec/exec_capture take an
          array on stdin (can); template_push takes one text (N texts
          CAN be concatenated); ``pipeline_launch``'s receiver takes ONE
          ``input: dict`` (``render.py``'s own
          ``render_pipeline_input``'s return type) — no merge of N dicts
          into one is lossless in general (any merge strategy silently
          drops N-1 events' worth of fields, the exact "捨てるのはバグ"
          owner named). So this scheme launches ONCE PER EVENT in the
          batch, unconditionally — the fold flag has NO EFFECT on this
          scheme. This is deliberate, not an oversight: a future
          implementer adding folding here needs a CHANGED pipeline input
          contract (a genuinely separate, larger issue than #5516), not a
          cleverer merge function.

        #3226 Phase 4 renamed ``shell_exec``/``shell_push`` → ``exec``/
        ``exec_capture`` (naming honesty — the runner never ran
        ``/bin/sh -c <string>``; it always argv-executed via
        ``shell=False``) and the payload from a shell-command string to an
        argv list (``HookDef.exec``/``HookDef.exec_capture`` are now
        ``tuple[str, ...]``)."""
        if hook.template_push is not None:
            resolved = _render_and_fold_pushes(hook.template_push, events, point)
            if resolved is not None:
                await self._push_resolved(resolved, hook, point)
        elif hook.exec is not None:
            # exec — an external side-effect. Output IGNORED; never raises
            # (the runner logs + returns). Backend: the injected instance, else
            # run_shell_hook resolves get_default_backend(sandbox_config).
            await self._run_shell(
                hook.exec,
                _build_event_context(events, skipped_session_wide),
                sandbox_backend=self._sandbox_backend,
                sandbox_config=self._sandbox_config,
                # #2827/#3005: the operator's per-hook sandbox knobs. None =
                # omitted = the runner keeps that axis at its floor (today's
                # behaviour). The agent-level sandbox.policy does not reach a
                # hook exec, so these keys are the operator's whole surface —
                # threading only some of them would leave the rest of the
                # asymmetry the issue names in place.
                allow_subprocess=hook.subprocess,
                network=hook.network,
                write_paths=hook.write_paths,
                consent_bus=self._consent_bus_now(),
                hook_name=hook.name,
                emit_event=self._emit_event,
                # #5084 ④: read live, same as consent_bus_now() above — a
                # relative exec argv resolves inside the agent's OWN tree.
                cwd=self._hook_cwd() if self._hook_cwd is not None else None,
                temp_dir=self._hook_temp_dir() if self._hook_temp_dir is not None else None,
                hook_process_context=(
                    self._hook_process_context()
                    if self._hook_process_context is not None
                    else None
                ),
            )
        elif hook.exec_capture is not None:
            # exec_capture (#2069) — an argv whose STDOUT is a JSON
            # push-directive. Captured (capture_stdout, vs exec's ignored
            # output), parsed fail-safe into a ResolvedPush, then dispatched via
            # the SAME C/E path as template_push. The ONLY difference from
            # template_push is the SOURCE of the ResolvedPush: stdout JSON here vs
            # Jinja2 render there. A run-failure (→ stdout None) or a parse-failure
            # (→ _parse_exec_push None) skips the push (fail-safe).
            stdout = await self._run_shell(
                hook.exec_capture,
                _build_event_context(events, skipped_session_wide),
                sandbox_backend=self._sandbox_backend,
                sandbox_config=self._sandbox_config,
                capture_stdout=True,
                # #2827/#3005: the same knobs on the exec_capture sibling — what
                # an argv needs from the sandbox is a property of the command,
                # not of which scheme consumes its stdout.
                allow_subprocess=hook.subprocess,
                network=hook.network,
                write_paths=hook.write_paths,
                consent_bus=self._consent_bus_now(),
                hook_name=hook.name,
                emit_event=self._emit_event,
                # #5084 ④: same live cwd/env source as the exec branch above.
                cwd=self._hook_cwd() if self._hook_cwd is not None else None,
                temp_dir=self._hook_temp_dir() if self._hook_temp_dir is not None else None,
                hook_process_context=(
                    self._hook_process_context()
                    if self._hook_process_context is not None
                    else None
                ),
                # #5210: live-resolved (cap_tokens, model) or None — see
                # shell_runner.run_shell_hook's own docstring for the
                # explicit-failure-not-truncation contract this enforces.
                output_token_cap=(
                    self._resolve_exec_capture_output_cap()
                    if self._resolve_exec_capture_output_cap is not None
                    else None
                ),
            )
            resolved = _parse_exec_push(stdout)
            if resolved is not None:
                await self._push_resolved(resolved, hook, point)
        elif hook.pipeline_launch is not None:
            # pipeline_launch (#2608 H3) — does NOT fold, see this method's
            # own docstring. Launch ONCE PER EVENT in the batch,
            # unconditionally.
            for event in events:
                await self._launch_one_pipeline(hook, point, event.payload)

    async def _launch_one_pipeline(self, hook: HookDef, point: str, template_vars: dict) -> None:
        """The unfolded, single-event pipeline_launch body (#2608 H3) —
        render input_template against ``template_vars``, then hand off to
        the injected ``launch_pipeline`` callable (async/detached — the
        result returns later on this session's own inbox as a
        ``pipeline_result`` message, same as the ``run_pipeline_async``
        tool verb). Fail-safe on either failure mode: a render error (bad
        Jinja2 / non-JSON-object output) or no ``launch_pipeline``
        callable injected both log a clear WARNING and skip the launch —
        never raise out of this method (the caller's per-hook isolation
        would catch it anyway, but a specific message here names exactly
        what went wrong)."""
        assert hook.pipeline_launch is not None
        try:
            input_data = render_pipeline_input(
                hook.pipeline_launch.input_template, template_vars,
            )
        except Exception as exc:  # noqa: BLE001 — render failure must not crash; skip launch
            _log.warning(
                "Hook pipeline_launch input_template render failed — launch "
                "skipped. hook=%r pipeline=%r error=%s: %s",
                hook.name, hook.pipeline_launch.name, type(exc).__name__, exc,
            )
            return
        if self._launch_pipeline is None:
            _log.warning(
                "Hook %r declares pipeline_launch (pipeline=%r) but this "
                "session's HookDispatcher has no launch_pipeline callable "
                "injected — launch skipped.",
                hook.name or point, hook.pipeline_launch.name,
            )
            return
        await self._launch_pipeline(hook.pipeline_launch.name, input_data)

    async def _push_resolved(self, resolved, hook: HookDef, point: str) -> None:
        """Dispatch a resolved push directive via C/E (#1800 slice 5b/6) — shared
        by ``template_push`` (Jinja2 render) and ``exec_capture`` (stdout JSON), so
        the only difference between the two is where ``resolved`` comes from."""
        if not resolved.push_when:
            return  # conditional push guard (or a render/parse failure — fail-safe)
        # #5514 §5/§8 (architect ruling, 2026-08-30): a spillability=never
        # hook's push exceeding its own declared spillability_max_chars is
        # REJECTED outright — never truncated. NEVER's own definition is
        # "losing it changes the remaining meaning"; a truncated frame
        # reads to the model as COMPLETE (a lie), which is worse than no
        # frame at all — and NEVER forbids offload by definition, so there
        # is no MediaStore ref to keep a truncated remainder lossless with
        # (`tool_result_offloaded`'s own lossless+ref shape does not apply
        # here). Scope: only this ONE push is dropped — the hook is not
        # disabled and the turn does not fail (a separate policy, not
        # this fix's scope). `first_choice`/`last_resort` hooks never
        # carry `spillability_max_chars` (loader.py's own load-time
        # requirement), so this branch is unreachable for them.
        if (
            hook.spillability is Spillability.NEVER
            and hook.spillability_max_chars is not None
            and len(resolved.message) > hook.spillability_max_chars
        ):
            _log.warning(
                "hook %r (spillability=never) push REJECTED — %d chars "
                "exceeds its declared spillability_max_chars=%d. The push "
                "is dropped, not truncated (a partial frame would read as "
                "complete to the model). History is unchanged.",
                hook.name or point, len(resolved.message),
                hook.spillability_max_chars,
            )
            if self._emit_event is not None:
                try:
                    self._emit_event(
                        "hook_push_rejected_oversized",
                        hook_name=hook.name,
                        declared_max_chars=hook.spillability_max_chars,
                        actual_chars=len(resolved.message),
                    )
                except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
                    _log.debug(
                        "hook push_rejected_oversized emit_event failed for %r: %s",
                        hook.name, exc,
                    )
            return
        # #2608 observability: every push FIRE (template_push's Jinja2 render or
        # exec_capture's stdout JSON, both funnel through here) is surfaced as a P6
        # event — previously ONLY exec/exec_capture emitted `hook_shell_executed`
        # on the RUN side; a push's only artifact was the WAL `inbox_put`/staged
        # context, so a push that fired but never drained (sat in the inbox forever)
        # left no EventLog trace at all. `hook_push_fired` closes that gap: metadata
        # only (hook_name/point/wake/target_session) — NEVER the rendered message
        # body, which may carry secrets from template_vars. Best-effort: a sink
        # error must never break the push (mirrors shell_runner's emit_event guard).
        if self._emit_event is not None:
            try:
                self._emit_event(
                    "hook_push_fired",
                    hook_name=hook.name,
                    point=point,
                    wake=resolved.wake,
                    target_session=(
                        resolved.session.strip()
                        if resolved.session and resolved.session.strip()
                        else self._current_session_id
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
                _log.debug("hook push_fired emit_event failed for %r: %s", hook.name, exc)
        # Attribution name (#1800 slice 6): the hook's operator label when set,
        # else the lifecycle point (slice-5b default) — the ``[hook:<name>]``
        # system-role prefix (shared E + C renderer). ``spillability`` rides
        # in the SAME payload dict both the wake=true (``_handle_hook_
        # message``) and wake=false (``_handle_inbox_text``'s ride-along)
        # consumers read from — the ONE construction site #5514 §8 requires
        # so the declaration cannot reach one mouth and silently miss the
        # other. ``None`` (undeclared) resolves to FIRST_CHOICE here, not
        # deferred to a consumer default — see ``HookDef.spillability``'s
        # own docstring for why FIRST_CHOICE, not ``Spillability.default()``.
        payload = {
            "name": hook.name or point,
            "text": resolved.message,
            "spillability": (hook.spillability or Spillability.FIRST_CHOICE).value,
        }
        # #2072: cross-session push. A ``resolved.session`` naming a DIFFERENT session routes
        # to THAT session's inbox (the canonical wake-triple); ``wake`` rides in the payload
        # so the target processes it the same way the current session would (wake → triggers a
        # turn; else → a passive ride-along on the target's next turn). No target / same
        # session / no cross-session capability → the local path below (unchanged).
        target = resolved.session.strip() if resolved.session else None
        if (target and self._cross_session_put is not None
                and target != self._current_session_id):
            await self._cross_session_put(
                target, TurnOrigin.HOOK, {**payload, "wake": resolved.wake}, wake=resolved.wake)
            return
        if resolved.wake:
            # E — a turn trigger (self-continuation): _put_inbox wake=True →
            # _drain_to_wake treats it as the trigger → _handle_hook_message.
            await self._put_inbox(TurnOrigin.HOOK, {**payload, "wake": True})
        else:
            # C — a passive ride-along: stage directly into next-turn context (the
            # 4b API), NOT via the inbox (a wake=false-only inbox push never drains
            # alone — Decision A).
            await self._stage_next_turn_context(HOOK_STAGE_KIND, payload)


def _build_event_context(events: "list[HookEvent]", skipped_session_wide: int) -> dict:
    """#5516 §1/§2 — the wire shape for an exec/exec_capture hook's stdin
    JSON. ``events`` (never empty) becomes the ``"events"`` array — always
    an array, even a length-1 batch (clean break, no dual shape: a script
    reading this never has to branch on "is this a dict or a list").
    ``skipped_session_wide`` rides as a SIBLING key, never folded into the
    array itself — it is session-scoped, not per-item (see
    ``HookDispatcher._dispatch_batch_for_point``'s own docstring for why
    it cannot be attributed to any one event)."""
    return {
        "events": [event.payload for event in events],
        "skipped_session_wide": skipped_session_wide,
    }


def _render_and_fold_pushes(
    push, events: "list[HookEvent]", point: str,
) -> "ResolvedPush | None":
    """#5516 §1 item ③ (owner ruling) — render ``push`` once PER event in
    the batch (via the unchanged single-event ``render_push``), then
    concatenate the N resolved messages into ONE :class:`ResolvedPush`.
    Returns ``None`` when every render in the batch skips (``push_when``
    false or a render failure — ``render_push``'s own fail-safe), so the
    caller never pushes an empty message.

    Two merge choices that are NOT specified anywhere in #5516's own
    canonical spec — stated explicitly here rather than silently picked,
    same posture as the pipeline_launch exemption above:

    - ``wake``: ``True`` if ANY resolved push in the batch wants to wake
      (never silently downgrades one event's genuine wake request because
      it happened to be folded alongside quieter ones).
    - ``session``: the FIRST resolved push's non-empty ``session`` (a
      hook's own template rarely varies target session per-event; if it
      ever does, later events' session targets are not represented — this
      is a real, named limitation, not a guess dressed as a decision)."""
    resolved_parts: "list[ResolvedPush]" = []
    for event in events:
        resolved = render_push(push, event.payload, point)
        if resolved.push_when:
            resolved_parts.append(resolved)
    if not resolved_parts:
        return None
    return ResolvedPush(
        message="\n".join(part.message for part in resolved_parts),
        wake=any(part.wake for part in resolved_parts),
        push_when=True,
        session=next(
            (part.session for part in resolved_parts if part.session), None,
        ),
    )


def _parse_exec_push(stdout: str | None) -> ResolvedPush | None:
    """Parse an ``exec_capture`` argv's captured stdout (a JSON push-directive,
    #2069) into a ``ResolvedPush``, or ``None`` to skip the push.

    Contract: stdout is a single JSON object
    ``{"push_when": bool, "wake": bool, "message": str, "session"?: str}``.
    The first three are required; ``session`` is optional.

    **Fail-safe** (never raises): empty stdout, invalid JSON, a non-object, a
    missing or wrong-typed required field, or a non-string ``session`` all log a
    WARNING and return ``None`` so the dispatcher skips the push and the run
    proceeds — symmetric with ``render_push``'s ``push_when=False`` safety net.

    ``session`` is parsed and carried on the ``ResolvedPush`` and (since #2072) ROUTED:
    a ``session`` naming a different live session delivers the push to THAT session's
    inbox (cross-session), exactly as for ``template_push``; ``null``/empty stays local.
    """
    if not stdout or not stdout.strip():
        return None
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        _log.warning(
            "exec_capture stdout is not valid JSON — push skipped. error=%s: %s",
            type(exc).__name__, exc,
        )
        return None
    if not isinstance(obj, dict):
        _log.warning(
            "exec_capture directive must be a JSON object, got %s — push skipped.",
            type(obj).__name__,
        )
        return None

    message = obj.get("message")
    wake = obj.get("wake")
    push_when = obj.get("push_when")
    session = obj.get("session")

    # Required-field + type checks (bool first — bool is an int subclass, so the
    # isinstance(..., bool) guard correctly rejects an integer 1/0).
    if not isinstance(push_when, bool):
        _log.warning("exec_capture directive 'push_when' must be a JSON bool — push skipped.")
        return None
    if not push_when:
        _log.debug("exec_capture directive declined push_when=false; push skipped.")
        return ResolvedPush(message="", wake=False, push_when=False, session=None)
    if not isinstance(message, str) or not message.strip():
        _log.warning("exec_capture directive 'message' must be a non-empty string — push skipped.")
        return None
    if not isinstance(wake, bool):
        _log.warning("exec_capture directive 'wake' must be a JSON bool — push skipped.")
        return None
    if session is not None and not isinstance(session, str):
        _log.warning("exec_capture directive 'session' must be a string or null — push skipped.")
        return None
    session = session if (session and session.strip()) else None

    return ResolvedPush(message=message, wake=wake, push_when=push_when, session=session)


__all__ = ["HookDispatcher", "HOOK_STAGE_KIND"]
