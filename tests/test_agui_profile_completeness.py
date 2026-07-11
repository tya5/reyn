"""Tier 2: every display kind is dispositioned on the AG-UI wire (P4/P6a/P6b).

This gate keeps the reyn AG-UI surface honest against the SETTLED closed vocabulary
(:data:`reyn.runtime.outbox.VOCABULARY` — declared in ``outbox.py``, enforced at
construction by ``OutboxMessage.__post_init__``). P6b retired the earlier AST
producer-scan: the vocabulary IS now the authoritative producer domain (a kind
outside it cannot be constructed), so the gate derives from it directly — and
binds it, non-circularly, against the REAL codec + profile registry (never the
vocabulary against itself).

Every vocabulary kind must be **dispositioned** as exactly one of:

- **standard-mapped** — the codec emits a standard AG-UI event (``agent`` /
  ``status`` / ``reasoning`` / ``error``); or
- **profiled** — the codec emits a ``CUSTOM`` ``reyn.*`` name that has an
  extension-profile entry; or
- **control-filtered** — the kind is in the explicit
  :data:`~reyn.interfaces.transport.agui.protocol.CONTROL_FILTER_KINDS` allowlist,
  so the emitter never puts it on the wire (a local-control sentinel).

A vocabulary kind outside all three ⇒ RED — an unprofiled Custom name would leak
on the wire, exactly the doc-drift this gate exists to catch. Real instances only
— the real codec + the real profile registry; no mocks.
"""
from __future__ import annotations

from reyn.core.events.events import Event
from reyn.interfaces.transport.agui.profile import (
    CUSTOM_PROFILE,
    DISPLAY_NS,
    is_profiled,
    profiled_names,
)
from reyn.interfaces.transport.agui.protocol import (
    CONTROL_FILTER_KINDS,
    CUSTOM,
    REASONING_MESSAGE_CONTENT,
    decode_event,
    encode_frame,
    encode_intervention_tool_start,
)
from reyn.interfaces.transport.frames import (
    DisplayFrame,
    EventFrame,
    renderer_chat_events,
)
from reyn.runtime.outbox import CONTROL_KINDS, DISPLAY_KINDS, VOCABULARY, OutboxMessage


def _disposition(kind: str) -> str:
    """Classify a vocabulary kind: 'standard' | 'profiled' | 'control' | 'LEAK'."""
    if kind in CONTROL_FILTER_KINDS:
        return "control"
    ev = encode_frame(DisplayFrame(OutboxMessage.from_wire(kind=kind, text="x")))
    if ev.type != CUSTOM:
        return "standard"
    if is_profiled(ev.data.get("name", "")):
        return "profiled"
    return "LEAK"


def _emitted_custom_names() -> set[str]:
    """The ``reyn.*`` Custom names the codec puts on the wire for the FORWARDED
    display vocabulary (:data:`DISPLAY_KINDS`) + the derived chat-event
    vocabulary. Reads the codec's output, never the profile."""
    names: set[str] = set()
    for kind in DISPLAY_KINDS:  # every wire-forwarded kind (control-filtered excluded)
        ev = encode_frame(DisplayFrame(OutboxMessage.from_wire(kind=kind, text="x")))
        if ev.type == CUSTOM:
            names.add(ev.data["name"])
    for etype in renderer_chat_events():
        ev = encode_frame(EventFrame(Event(type=etype, data={})))
        if ev.type == CUSTOM:
            names.add(ev.data["name"])
    return names


def test_every_vocabulary_kind_is_dispositioned() -> None:
    """Tier 2: every closed-vocabulary kind is dispositioned (converted from the P6a AST scan);
    standard-mapped OR profiled OR in the explicit control-filter allowlist.
    Anything else is an unprofiled Custom name that would leak on the wire ⇒ RED.

    Strip-falsify (recorded in the PR): dropping the ``reyn.display.trace`` (or any
    profiled) entry, or removing a kind from ``CONTROL_FILTER_KINDS``, makes that
    kind a LEAK ⇒ RED."""
    # Sanity: the vocabulary spans all three dispositions (a broken/empty
    # vocabulary must not vacuously pass).
    assert {"agent", "status", "reasoning", "error"} <= VOCABULARY   # standard
    assert {"system", "user", "trace", "__attach_request__",
            "tool_call_started"} <= VOCABULARY                       # profiled CUSTOM
    assert {"__end__", "__session_switch_request__"} <= VOCABULARY   # control-filtered

    leaks = {k for k in VOCABULARY if _disposition(k) == "LEAK"}
    assert not leaks, (
        "vocabulary kinds with no disposition (standard-map, profile, or add to "
        f"CONTROL_FILTER_KINDS): {sorted(leaks)}"
    )


def test_control_kinds_match_the_wire_filter_allowlist() -> None:
    """Tier 2: the outbox ``CONTROL_KINDS`` (emitter-filtered sentinels) are
    EXACTLY the codec's ``CONTROL_FILTER_KINDS`` — the two independent declarations
    of "never forwarded on the wire" stay bound. Drift either way ⇒ RED."""
    assert CONTROL_KINDS == CONTROL_FILTER_KINDS


def test_control_sentinel_dispositions_client_consumed_forward_upstream_consumed_filter() -> None:
    """Tier 2: the per-entry disposition of the `__…__` control sentinels.

    - ``__copy_last_reply__`` / ``__rewind_list__`` are **client-consumed** over the
      transport stream (real client-side clipboard copy / rewind picker), so they
      are FORWARDED (profiled CUSTOM display kinds) and round-trip losslessly.
    - ``__end__`` (terminal) and ``__session_switch_request__`` (upstream-consumed)
      are control-filtered.
    - ``__attach_request__`` is upstream-consumed; its profile entry is a fail-safe."""
    # Client-consumed → forwarded + profiled + lossless round-trip.
    for client_kind in ("__copy_last_reply__", "__rewind_list__"):
        assert client_kind in DISPLAY_KINDS, client_kind
        assert client_kind not in CONTROL_FILTER_KINDS, client_kind
        assert _disposition(client_kind) == "profiled", client_kind
        ev = encode_frame(DisplayFrame(OutboxMessage(kind=client_kind, text="x")))
        assert ev.data["name"] == f"reyn.display.{client_kind}"
        decoded = decode_event(ev.type, ev.data)
        assert isinstance(decoded, DisplayFrame)
        assert decoded.message.kind == client_kind

    # Terminal + upstream-consumed → control-filtered.
    assert _disposition("__end__") == "control"
    assert _disposition("__session_switch_request__") == "control"

    # __attach_request__ profile entry is a fail-safe (kept profiled).
    assert "__attach_request__" not in CONTROL_FILTER_KINDS
    assert _disposition("__attach_request__") == "profiled"
    assert is_profiled("reyn.display.__attach_request__")


def test_every_custom_mapped_frame_is_profiled() -> None:
    """Tier 2: each reyn.* Custom name the codec emits (for a forwarded display
    kind) has an extension-profile entry. An unprofiled Custom name ⇒ RED."""
    emitted = _emitted_custom_names()

    # Sanity: the enumeration found the real emitted Custom vocabulary.
    assert {
        "reyn.display.system",
        "reyn.display.trace",
        "reyn.display.__attach_request__",
        "reyn.display.tool_call_started",
        "reyn.event.user_answered_intervention",
    } <= emitted

    missing = {name for name in emitted if not is_profiled(name)}
    assert not missing, f"unprofiled reyn.* Custom names (add to profile): {sorted(missing)}"


def test_no_dead_display_profile_entry() -> None:
    """Tier 2: every ``reyn.display.<kind>`` profile (reverse dead-entry catch)
    entry names a kind that is in the closed vocabulary — no profile entry for a
    kind no producer can construct. Drift (a stale profile member) ⇒ RED."""
    prefix = f"{DISPLAY_NS}."
    for name in CUSTOM_PROFILE:
        if not name.startswith(prefix):
            continue
        kind = name[len(prefix):]
        assert kind in VOCABULARY, (
            f"profile entry {name!r} names kind {kind!r} which is not in the closed "
            "vocabulary (dead profile entry — remove it or add the kind)"
        )


def test_intervention_frontend_tool_toolname_is_profiled() -> None:
    """Tier 2: the HITL frontend-tool ``toolName`` the emitter really produces
    falls under a profiled reyn.intervention.* namespace — for every intervention
    kind (open namespace). Unprofiled ⇒ RED."""
    for kind in ("ask_user", "permission.grant_deny"):
        ev = encode_intervention_tool_start(
            {"intervention_id": "iv-1", "intervention_kind": kind, "prompt": "?"}
        )
        toolname = ev.data["toolName"]
        assert toolname.startswith("reyn.intervention."), toolname
        assert is_profiled(toolname), f"unprofiled intervention frontend-tool: {toolname}"

    ev = encode_intervention_tool_start({"intervention_id": "iv-2", "intervention_kind": ""})
    assert is_profiled(ev.data["toolName"])


def test_reasoning_is_standard_mapped_not_a_custom_profile_entry() -> None:
    """Tier 2: P6a — the reasoning frame is STANDARD-mapped (a Reasoning* event),
    so it needs NO reyn.* Custom profile entry.

    Strip-falsify: remove ``"reasoning"`` from ``_DISPLAY_KIND_EVENT`` → the frame
    reverts to CUSTOM (``reyn.display.reasoning``) → the standard-mapped assertion
    below goes RED."""
    ev = encode_frame(DisplayFrame(OutboxMessage(kind="reasoning", text="thinking")))

    assert ev.type == REASONING_MESSAGE_CONTENT
    assert ev.type != CUSTOM
    assert "name" not in ev.data, "a standard Reasoning* event carries no CUSTOM name"

    assert "reyn.display.reasoning" not in profiled_names()
    assert "reyn.display.reasoning" not in _emitted_custom_names()
