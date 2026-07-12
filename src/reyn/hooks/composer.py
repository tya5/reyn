"""reyn.hooks.composer — the Composer (Hook-Event Redesign Phase 4b, proposal
``docs/deep-dives/proposals/0059-hook-event-redesign.md`` §5).

A Composer subscribes to a per-Session :class:`~reyn.hooks.bus.HookBus`
(Phase 4a), buffers/correlates incoming :class:`~reyn.hooks.event.HookEvent`
instances per its configured op, and — when the op's condition is met —
publishes ONE new composed :class:`HookEvent` back to the SAME bus, with
``kind = "composed:<name>"``. This module never touches P6 audit-events or
WAL-events (CLAUDE.md's 3-event rule) and never constructs/replaces
``HookDispatcher`` — it is a Bus-only reactivity layer built entirely on top
of the Phase 4a Bus.

Ops (§5, all seven implemented)
--------------------------------
``all``          — fires once every one of N distinct inputs has arrived
                   (per correlation key).
``any``          — fires immediately on the first matching input; stateless,
                   no pending record.
``seq``          — fires once its inputs' KINDS have arrived in the
                   CONFIGURED ORDER (per key); an out-of-order match resets
                   progress for that key.
``window``       — buffers every matching event for ``ttl`` seconds after the
                   FIRST one (per key), then fires with the whole buffer.
``debounce``      — fires ``ttl`` seconds after the LAST matching event with
                   no newer one arriving in between (per key) — a trailing
                   debounce, not a leading one.
``correlate_by``  — like ``all``, but the correlation KEY is read from
                   ``payload[correlate_by]`` instead of a single global
                   bucket (lets independent instances of a multi-input
                   correlation run concurrently, keyed by e.g. a request id).
``count``        — fires once ``threshold`` matching events have arrived
                   (per key); the count then resets for that key.

Source-seam degeneracy (architect-ratified, proposal §5/§3.2)
----------------------------------------------------------------
On the ``HookBus`` every :class:`HookEvent` carries ``source="builtin"`` —
``HookDispatcher.dispatch`` never sets anything else (see
``reyn.hooks.event.HookEvent.source`` and ``reyn.hooks.event_pattern.
EventPattern.source`` docstrings for the full rationale: ``kind`` already
encodes the source TYPE and ``payload`` retains the source INSTANCE —
``payload.server`` / ``payload.path`` / ``payload.job_name`` /
``payload.transport``). **A Composer input's ``source`` predicate is
therefore degenerate in this phase** — correlate on a payload field, never on
``source``. ``ComposerInput`` REJECTS (fail-loud, at config-load time) any
``source`` value other than ``"builtin"``/``None``, the same typo-resistance
posture Phase 3's ``EventPattern.validate_against_schema`` established for
matcher fields, so a Composer author writing ``source: "mcp:github"`` (a
value that can never match, since the Bus never carries it) gets a load-time
error instead of a silently-dead input.

The five §5 invariants (architect-ratified review gate for this phase)
--------------------------------------------------------------------------
1. **PendingStore seam**: :class:`PendingRecord` is a plain dataclass of
   primitives + a list of (immutable, dict-payload) ``HookEvent`` — JSON-
   serializable, so a future ``WalBackedPendingStore`` is a drop-in swap of
   the :class:`PendingStore` protocol, not a rewrite. The moment a
   WAL-backed store lands, CLAUDE.md's recovery-feature PR gate fires (a
   truncate-falsify test: set pending X -> truncate the WAL below X's
   events -> reconstruct -> assert X survives). :class:`InMemoryPendingStore`
   (this phase's only implementation) is explicitly **best-effort /
   crash-non-durable** — a process crash silently drops every in-flight
   correlation, by design, not as an oversight (proposal §5 Q-reyn-1,
   owner-ratified: "best-effort now, WAL-backed later").
2. **QueuePolicy excludes Backpressure** (``DropOldest`` / ``DropNewest`` /
   ``Reject`` only — proposal §5 review-pass): the Bus's OWN input is
   already drop-oldest-lossy (``HookBus.publish``), so a Composer — built
   entirely on subscribing to that Bus — **cannot be more reliable than its
   input** and must never promise exactly-once or complete-window delivery.
   Backpressure (publisher-blocking) is incompatible with the Bus's
   never-blocks-the-publisher broadcast contract and is explicitly deferred.
3. **P6 visibility is metadata-only**: every fire emits ``composer_fired``
   and every drop emits ``composer_dropped`` (capacity overflow, ttl-evict,
   or explicit reject) through the SAME best-effort ``emit_event`` sink
   ``HookDispatcher`` already uses for ``hook_push_fired``/
   ``hook_shell_executed`` — metadata (composer name + correlation key +
   reason) ONLY, never the composed payload's CONTENT (a webhook-derived
   field could carry PII/secrets). Eviction on capacity overflow or ttl
   ALWAYS emits ``composer_dropped`` — no silent drop path exists.
4. **Cycle-check is a load-time DAG check** (:func:`check_no_cycles`,
   Phase-3-style fail-loud, never a runtime check): a ``composed:<name>``
   input feeding another composer can form a cycle; this is caught by
   :func:`load_composers` before any Composer starts running.
5. **Sync-origin non-composition + no Sync re-entry**: a Composer only ever
   calls ``HookBus.publish`` — it never touches ``HookDispatcher`` or
   ``HookRegistry.hooks_for``. A composed event is Bus-only observable in
   this phase; it is NOT looped back into Sync dispatch (that would let
   composed -> Sync -> re-dispatch bypass ``max_hook_driven_turns``, an
   unbounded reactivity amplification path). Wiring a ``composed:*`` kind as
   a Sync ``hooks:`` entry (proposal §9's illustrative config) is
   deliberately OUT OF SCOPE for this phase — the composition graph's
   leaves do not re-enter Sync dispatch. #4 and #5 are ONE Reliability
   invariant: the composition graph is a finite DAG whose leaves never
   re-enter Sync dispatch.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from reyn.hooks.bus import HookBus
from reyn.hooks.event import HookEvent
from reyn.hooks.event_pattern import EventPattern
from reyn.hooks.event_pattern import matches as _pattern_matches

_log = logging.getLogger(__name__)

EmitEvent = Callable[..., Any]

# Composed hook-events are namespaced ``composed:<name>`` (proposal §5) —
# distinct from ``builtin:*`` / ``mcp:*`` / ``webhook:*`` / ``llm:*``.
COMPOSED_KIND_PREFIX = "composed:"


class ComposerConfigError(ValueError):
    """A ``composers:`` config entry is structurally invalid, names an
    unknown op, or (with a sibling composer) forms a composition cycle.
    Raised at load time (``load_composers``) — fail-loud, never at runtime."""


class ComposerOp(str, Enum):
    """The seven §5 composition ops."""

    ALL = "all"
    ANY = "any"
    SEQ = "seq"
    WINDOW = "window"
    DEBOUNCE = "debounce"
    CORRELATE_BY = "correlate_by"
    COUNT = "count"


class QueuePolicy(str, Enum):
    """Overflow policy for a Composer's pending state (§5 review-pass —
    ``Backpressure`` explicitly excluded from v1, see module docstring
    invariant #2)."""

    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    REJECT = "reject"


_DEFAULT_TTL_SECONDS = 300.0  # 5m, matching the proposal §9 example
_DEFAULT_CAPACITY = 10  # matching the proposal §9 example
_MAX_SWEEP_INTERVAL = 1.0


def _parse_duration(raw: "str | int | float") -> float:
    """Parse a duration — a plain number of seconds, or a ``<N><unit>``
    string with unit ``s``/``m``/``h`` (proposal §9 example: ``"5m"``)."""
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    units = {"s": 1.0, "m": 60.0, "h": 3600.0}
    if text and text[-1] in units:
        try:
            return float(text[:-1]) * units[text[-1]]
        except ValueError:
            raise ComposerConfigError(f"invalid duration {raw!r}") from None
    try:
        return float(text)
    except ValueError:
        raise ComposerConfigError(f"invalid duration {raw!r}") from None


@dataclass(frozen=True)
class ComposerInput:
    """One input slot of a Composer — a :class:`~reyn.hooks.event_pattern.
    EventPattern` (kind/payload; see module docstring for why ``source`` is
    degenerate and therefore restricted here) plus the kind string used for
    ``seq`` ordering / display."""

    kind: str
    pattern: EventPattern

    def matches(self, event: HookEvent) -> bool:
        return _pattern_matches(self.pattern, event)


@dataclass(frozen=True)
class ComposerPolicy:
    """Overflow/lifetime policy for a Composer's pending state."""

    capacity: int = _DEFAULT_CAPACITY
    overflow: QueuePolicy = QueuePolicy.DROP_OLDEST
    ttl_seconds: float = _DEFAULT_TTL_SECONDS


@dataclass(frozen=True)
class ComposerDef:
    """A single ``composers:`` config entry, fully parsed and validated."""

    name: str
    op: ComposerOp
    inputs: "tuple[ComposerInput, ...]"
    emit_kind: str
    policy: ComposerPolicy = field(default_factory=ComposerPolicy)
    correlate_by: "str | None" = None
    threshold: "int | None" = None


# ---------------------------------------------------------------------------
# PendingStore seam (invariant #1) — serializable / snapshot-friendly by
# construction (primitives + HookEvent, itself a frozen dataclass of
# primitives/dict). InMemoryPendingStore is v1's ONLY implementation and is
# explicitly best-effort / crash-non-durable (see module docstring).
# ---------------------------------------------------------------------------


@dataclass
class PendingRecord:
    """A single correlation key's in-flight buffered state. Every field is a
    JSON-primitive or a list of :class:`HookEvent` — deliberately snapshot
    shaped so a future ``WalBackedPendingStore`` can serialize/restore it
    without a redesign (invariant #1)."""

    events: "list[HookEvent]" = field(default_factory=list)
    matched_inputs: "set[int]" = field(default_factory=set)
    seq_pos: int = 0
    created_at: float = field(default_factory=time.time)
    last_at: float = field(default_factory=time.time)


class PendingStore(Protocol):
    """Seam for a Composer's per-key pending state (invariant #1). V1 ships
    only :class:`InMemoryPendingStore`; a future ``WalBackedPendingStore``
    implements the same protocol so the Composer never has to change."""

    def get(self, composer: str, key: str) -> "PendingRecord | None": ...
    def put(self, composer: str, key: str, record: PendingRecord) -> None: ...
    def delete(self, composer: str, key: str) -> None: ...
    def keys(self, composer: str) -> "list[str]": ...


class InMemoryPendingStore:
    """v1 ``PendingStore`` — a plain in-process dict. **Crash-non-durable by
    design** (proposal §5 Q-reyn-1, owner-ratified best-effort-now posture):
    a process crash silently discards every in-flight correlation with no
    reconstruction. This is NOT a recovery feature (CLAUDE.md's
    recovery-feature PR gate does not apply to this class) — the day a
    ``WalBackedPendingStore`` implementing the same :class:`PendingStore`
    protocol lands, THAT class is subject to the gate (truncate-falsify
    test: pending X survives truncation below X's events)."""

    def __init__(self) -> None:
        self._data: "dict[tuple[str, str], PendingRecord]" = {}

    def get(self, composer: str, key: str) -> "PendingRecord | None":
        return self._data.get((composer, key))

    def put(self, composer: str, key: str, record: PendingRecord) -> None:
        self._data[(composer, key)] = record

    def delete(self, composer: str, key: str) -> None:
        self._data.pop((composer, key), None)

    def keys(self, composer: str) -> "list[str]":
        return [k for (c, k) in self._data if c == composer]


_DEFAULT_CORRELATION_KEY = "__default__"


class Composer:
    """A single running Composer — subscribes to a :class:`HookBus`,
    correlates events per its :class:`ComposerDef`, and publishes a composed
    ``HookEvent`` back to the SAME bus on fire (invariant #5: Bus-only,
    never Sync). Construct via :func:`start_composers`."""

    def __init__(
        self,
        definition: ComposerDef,
        *,
        bus: HookBus,
        pending_store: "PendingStore | None" = None,
        emit_event: "EmitEvent | None" = None,
    ) -> None:
        self._def = definition
        self._bus = bus
        self._store: PendingStore = pending_store if pending_store is not None else InMemoryPendingStore()
        self._emit_event = emit_event

    def _audit(self, kind: str, **metadata: Any) -> None:
        """Fire a P6 metadata-only audit-event (invariant #3) through the
        SAME best-effort sink ``HookDispatcher`` already uses. Never raises
        (mirrors ``HookDispatcher._push_resolved``'s emit_event guard) and
        NEVER includes composed payload content — metadata only."""
        if self._emit_event is None:
            return
        try:
            self._emit_event(kind, composer=self._def.name, **metadata)
        except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
            _log.debug("Composer %r: emit_event(%r) failed: %s", self._def.name, kind, exc)

    def _correlation_key(self, event: HookEvent) -> str:
        if self._def.correlate_by is not None:
            value = event.payload.get(self._def.correlate_by)
            if value is None:
                return _DEFAULT_CORRELATION_KEY
            return str(value)
        return _DEFAULT_CORRELATION_KEY

    def _matched_input_indices(self, event: HookEvent) -> "list[int]":
        return [i for i, inp in enumerate(self._def.inputs) if inp.matches(event)]

    def _admit_new_key(self, key: str) -> bool:
        """Enforce ``policy.capacity`` over the NUMBER of distinct pending
        keys (invariant #2's QueuePolicy). Returns True iff ``key`` may be
        admitted as a new pending record."""
        existing = self._store.keys(self._def.name)
        if key in existing or len(existing) < self._def.policy.capacity:
            return True
        overflow = self._def.policy.overflow
        if overflow is QueuePolicy.DROP_OLDEST:
            oldest_key, oldest_ts = None, None
            for k in existing:
                rec = self._store.get(self._def.name, k)
                if rec is not None and (oldest_ts is None or rec.created_at < oldest_ts):
                    oldest_key, oldest_ts = k, rec.created_at
            if oldest_key is not None:
                self._store.delete(self._def.name, oldest_key)
                self._audit("composer_dropped", correlation_key=oldest_key, reason="capacity_drop_oldest")
            return True
        # DROP_NEWEST / REJECT: refuse the new key outright (v1 treats both
        # identically for a brand-new key — see module docstring invariant #2;
        # the distinction matters once buffer-level, not key-level, capacity
        # is added).
        reason = "capacity_drop_newest" if overflow is QueuePolicy.DROP_NEWEST else "capacity_reject"
        self._audit("composer_dropped", correlation_key=key, reason=reason)
        return False

    def _emit_composed(self, key: str, events: "list[HookEvent]") -> None:
        payload = {"inputs": [e.payload for e in events], "correlation_key": key}
        composed = HookEvent(kind=self._def.emit_kind, payload=payload, source="builtin")
        self._bus.publish(composed)  # invariant #5 — Bus-only, never dispatcher.dispatch
        self._audit("composer_fired", correlation_key=key)

    # -- op handlers ---------------------------------------------------

    def _handle_all_or_correlate(self, event: HookEvent, key: str, indices: "list[int]") -> None:
        record = self._store.get(self._def.name, key)
        if record is None:
            if not self._admit_new_key(key):
                return
            record = PendingRecord()
        record.events.append(event)
        record.matched_inputs.update(indices)
        record.last_at = time.time()
        if len(record.matched_inputs) >= len(self._def.inputs):
            self._store.delete(self._def.name, key)
            self._emit_composed(key, record.events)
        else:
            self._store.put(self._def.name, key, record)

    def _handle_seq(self, event: HookEvent, key: str) -> None:
        record = self._store.get(self._def.name, key)
        expected = self._def.inputs[record.seq_pos if record is not None else 0]
        if not expected.matches(event):
            return  # not the next-expected input for this key — ignore, don't disturb progress
        if record is None:
            if not self._admit_new_key(key):
                return
            record = PendingRecord()
        record.events.append(event)
        record.seq_pos += 1
        record.last_at = time.time()
        if record.seq_pos >= len(self._def.inputs):
            self._store.delete(self._def.name, key)
            self._emit_composed(key, record.events)
        else:
            self._store.put(self._def.name, key, record)

    def _handle_window(self, event: HookEvent, key: str) -> None:
        record = self._store.get(self._def.name, key)
        if record is None:
            if not self._admit_new_key(key):
                return
            record = PendingRecord()
        record.events.append(event)
        record.last_at = time.time()
        self._store.put(self._def.name, key, record)  # window closes on sweep, not here

    def _handle_debounce(self, event: HookEvent, key: str) -> None:
        record = self._store.get(self._def.name, key)
        if record is None:
            if not self._admit_new_key(key):
                return
            record = PendingRecord()
        record.events = [event]  # debounce only cares about the LAST matching event
        record.last_at = time.time()
        self._store.put(self._def.name, key, record)

    def _handle_count(self, event: HookEvent, key: str) -> None:
        record = self._store.get(self._def.name, key)
        if record is None:
            if not self._admit_new_key(key):
                return
            record = PendingRecord()
        record.events.append(event)
        record.last_at = time.time()
        threshold = self._def.threshold or 1
        if len(record.events) >= threshold:
            self._store.delete(self._def.name, key)
            self._emit_composed(key, record.events)
        else:
            self._store.put(self._def.name, key, record)

    def handle_event(self, event: HookEvent) -> None:
        """Feed one bus-observed ``HookEvent`` through this composer's op.
        Public (not just internal) so a test can drive a composer directly,
        without depending on the background subscription task's scheduling."""
        indices = self._matched_input_indices(event)
        if not indices:
            return
        key = self._correlation_key(event)
        op = self._def.op
        if op is ComposerOp.ANY:
            self._emit_composed(key, [event])
        elif op in (ComposerOp.ALL, ComposerOp.CORRELATE_BY):
            self._handle_all_or_correlate(event, key, indices)
        elif op is ComposerOp.SEQ:
            self._handle_seq(event, key)
        elif op is ComposerOp.WINDOW:
            self._handle_window(event, key)
        elif op is ComposerOp.DEBOUNCE:
            self._handle_debounce(event, key)
        elif op is ComposerOp.COUNT:
            self._handle_count(event, key)

    def sweep(self, *, now: "float | None" = None) -> None:
        """Time-driven half of the op set (invariant #2's TTL eviction +
        ``window``'s close + ``debounce``'s fire) — called periodically by
        the background run loop, and directly callable by tests for
        determinism (no real-time sleeping required)."""
        now = now if now is not None else time.time()
        ttl = self._def.policy.ttl_seconds
        for key in list(self._store.keys(self._def.name)):
            record = self._store.get(self._def.name, key)
            if record is None:
                continue
            if self._def.op is ComposerOp.WINDOW:
                if now - record.created_at >= ttl:
                    self._store.delete(self._def.name, key)
                    self._emit_composed(key, record.events)
            elif self._def.op is ComposerOp.DEBOUNCE:
                if now - record.last_at >= ttl:
                    self._store.delete(self._def.name, key)
                    self._emit_composed(key, record.events)
            else:  # ALL / SEQ / CORRELATE_BY / COUNT — ttl is an incomplete-pending evict
                if now - record.created_at >= ttl:
                    self._store.delete(self._def.name, key)
                    self._audit("composer_dropped", correlation_key=key, reason="ttl_evict")

    async def run(self) -> None:
        """Background task body: subscribe to the bus and feed every
        observed event through :meth:`handle_event`, sweeping periodically
        for ``window``/``debounce``/ttl-evict. Runs until cancelled."""
        sweep_interval = min(self._def.policy.ttl_seconds, _MAX_SWEEP_INTERVAL) or _MAX_SWEEP_INTERVAL
        async with self._bus.subscribe() as sub:
            while True:
                try:
                    event = await asyncio.wait_for(sub.get(), timeout=sweep_interval)
                except asyncio.TimeoutError:
                    self.sweep()
                    continue
                self.handle_event(event)


@dataclass
class ComposerRegistry:
    """The set of running Composers for one Session's Bus. Owns their
    background tasks; :meth:`stop` cancels all of them cleanly."""

    composers: "list[Composer]"
    _tasks: "list[asyncio.Task]" = field(default_factory=list)

    def start(self) -> None:
        self._tasks = [asyncio.ensure_future(c.run()) for c in self.composers]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []


def start_composers(
    definitions: "list[ComposerDef]",
    *,
    bus: HookBus,
    pending_store: "PendingStore | None" = None,
    emit_event: "EmitEvent | None" = None,
) -> ComposerRegistry:
    """Construct + start a :class:`ComposerRegistry` for every ``ComposerDef``
    against the same per-Session ``bus`` — the intended production entry
    point once a Session wires ``composers:`` config (config-wiring itself,
    e.g. threading this into ``runtime/session.py``, is a follow-up; this
    function is the seam that follow-up calls)."""
    registry = ComposerRegistry(
        composers=[
            Composer(d, bus=bus, pending_store=pending_store, emit_event=emit_event)
            for d in definitions
        ],
    )
    registry.start()
    return registry


# ---------------------------------------------------------------------------
# Config parsing + load-time cycle-check (invariant #4)
# ---------------------------------------------------------------------------


def _parse_input(raw: object, composer_name: str, index: int) -> ComposerInput:
    if not isinstance(raw, dict):
        raise ComposerConfigError(
            f"composers[{composer_name}].inputs[{index}] must be a mapping, got {type(raw).__name__!r}."
        )
    kind = raw.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise ComposerConfigError(f"composers[{composer_name}].inputs[{index}].kind is required.")
    match = raw.get("match")
    if match is not None and not isinstance(match, dict):
        raise ComposerConfigError(
            f"composers[{composer_name}].inputs[{index}].match must be a mapping."
        )
    source = raw.get("source")
    if source is not None and source != "builtin":
        # Source-seam degeneracy (module docstring): the Bus never carries
        # anything but "builtin" — a Composer author naming any other source
        # value would silently never match. Fail-loud instead (Phase-3
        # typo-resistance parallel).
        raise ComposerConfigError(
            f"composers[{composer_name}].inputs[{index}].source={source!r} can never match — "
            "the HookBus only ever carries source=\"builtin\" (kind + payload already encode the "
            "source instance; see reyn.hooks.event_pattern.EventPattern.source). Omit `source` "
            "and correlate on a payload field instead."
        )
    return ComposerInput(kind=kind, pattern=EventPattern(kind=kind, payload=match))


def _parse_policy(raw: object, composer_name: str) -> ComposerPolicy:
    if raw is None:
        return ComposerPolicy()
    if not isinstance(raw, dict):
        raise ComposerConfigError(f"composers[{composer_name}].policy must be a mapping.")
    capacity = raw.get("capacity", _DEFAULT_CAPACITY)
    if not isinstance(capacity, int) or capacity < 1:
        raise ComposerConfigError(f"composers[{composer_name}].policy.capacity must be a positive int.")
    overflow_raw = raw.get("overflow", QueuePolicy.DROP_OLDEST.value)
    try:
        overflow = QueuePolicy(overflow_raw)
    except ValueError:
        raise ComposerConfigError(
            f"composers[{composer_name}].policy.overflow={overflow_raw!r} — "
            f"must be one of {[p.value for p in QueuePolicy]} (Backpressure is v1-excluded)."
        ) from None
    ttl_raw = raw.get("ttl", _DEFAULT_TTL_SECONDS)
    ttl_seconds = _parse_duration(ttl_raw)
    if ttl_seconds <= 0:
        raise ComposerConfigError(f"composers[{composer_name}].policy.ttl must be positive.")
    return ComposerPolicy(capacity=capacity, overflow=overflow, ttl_seconds=ttl_seconds)


def _parse_one(raw: object, index: int) -> ComposerDef:
    if not isinstance(raw, dict):
        raise ComposerConfigError(f"composers[{index}] must be a mapping, got {type(raw).__name__!r}.")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ComposerConfigError(f"composers[{index}].name is required.")
    op_raw = raw.get("op")
    try:
        op = ComposerOp(op_raw)
    except ValueError:
        raise ComposerConfigError(
            f"composers[{name}].op={op_raw!r} — must be one of {[o.value for o in ComposerOp]}."
        ) from None
    raw_inputs = raw.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise ComposerConfigError(f"composers[{name}].inputs must be a non-empty list.")
    inputs = tuple(_parse_input(inp, name, i) for i, inp in enumerate(raw_inputs))
    emit_raw = raw.get("emit")
    if not isinstance(emit_raw, dict) or not isinstance(emit_raw.get("kind"), str):
        raise ComposerConfigError(f"composers[{name}].emit.kind is required.")
    emit_kind = emit_raw["kind"]
    if not emit_kind.startswith(COMPOSED_KIND_PREFIX):
        raise ComposerConfigError(
            f"composers[{name}].emit.kind={emit_kind!r} must start with {COMPOSED_KIND_PREFIX!r} "
            "(bare `emit`/non-namespaced kinds collide with the P6 audit-event surface — "
            "CLAUDE.md's 3-event naming rule)."
        )
    correlate_by = raw.get("correlate_by")
    if correlate_by is not None and not isinstance(correlate_by, str):
        raise ComposerConfigError(f"composers[{name}].correlate_by must be a string field name.")
    if op is ComposerOp.CORRELATE_BY and not correlate_by:
        raise ComposerConfigError(f"composers[{name}].op=correlate_by requires `correlate_by`.")
    threshold = raw.get("count")
    if threshold is not None and (not isinstance(threshold, int) or threshold < 1):
        raise ComposerConfigError(f"composers[{name}].count must be a positive int.")
    if op is ComposerOp.COUNT and not threshold:
        raise ComposerConfigError(f"composers[{name}].op=count requires `count` (the threshold).")
    if op is ComposerOp.SEQ and len(inputs) < 2:
        raise ComposerConfigError(f"composers[{name}].op=seq requires at least 2 inputs.")
    return ComposerDef(
        name=name, op=op, inputs=inputs, emit_kind=emit_kind,
        policy=_parse_policy(raw.get("policy"), name),
        correlate_by=correlate_by, threshold=threshold,
    )


def check_no_cycles(definitions: "list[ComposerDef]") -> None:
    """Fail-loud DAG check (invariant #4): a composer whose inputs include
    ANOTHER composer's ``emit.kind`` (a ``composed:*`` chain) must not form a
    cycle. Raises ``ComposerConfigError`` naming the cycle path — checked at
    load time, never at runtime (Phase-3-style static validation)."""
    emit_to_name = {d.emit_kind: d.name for d in definitions}
    by_name = {d.name: d for d in definitions}
    edges: "dict[str, list[str]]" = {
        d.name: [
            emit_to_name[inp.kind]
            for inp in d.inputs
            if inp.kind in emit_to_name and emit_to_name[inp.kind] != d.name
        ]
        for d in definitions
    }
    # also self-feed: a composer taking its own emit.kind as an input
    for d in definitions:
        if any(inp.kind == d.emit_kind for inp in d.inputs):
            raise ComposerConfigError(f"composer {d.name!r} feeds its own composed kind — a 1-cycle.")

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in by_name}
    path: "list[str]" = []

    def visit(name: str) -> None:
        color[name] = GRAY
        path.append(name)
        for dep in edges.get(name, []):
            if color[dep] == GRAY:
                cycle = path[path.index(dep):] + [dep]
                raise ComposerConfigError(
                    f"composer composition cycle detected: {' -> '.join(cycle)}"
                )
            if color[dep] == WHITE:
                visit(dep)
        path.pop()
        color[name] = BLACK

    for name in by_name:
        if color[name] == WHITE:
            visit(name)


def load_composers(raw: object) -> "list[ComposerDef]":
    """Parse + validate the ``composers:`` config block (proposal §9). Fail-
    loud (``ComposerConfigError``) on any structural issue OR a composition
    cycle (invariant #4) — never at runtime."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ComposerConfigError(f"composers must be a list, got {type(raw).__name__!r}.")
    definitions = [_parse_one(entry, i) for i, entry in enumerate(raw)]
    names = [d.name for d in definitions]
    if len(names) != len(set(names)):
        raise ComposerConfigError(f"duplicate composer name(s) in composers: {names}")
    check_no_cycles(definitions)
    return definitions


__all__ = [
    "COMPOSED_KIND_PREFIX",
    "Composer",
    "ComposerConfigError",
    "ComposerDef",
    "ComposerInput",
    "ComposerOp",
    "ComposerPolicy",
    "ComposerRegistry",
    "InMemoryPendingStore",
    "PendingRecord",
    "PendingStore",
    "QueuePolicy",
    "check_no_cycles",
    "load_composers",
    "start_composers",
]
