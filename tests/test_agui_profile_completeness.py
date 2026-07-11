"""Tier 2: every producer display kind is dispositioned on the AG-UI wire (P4/P6a).

This gate keeps the reyn AG-UI surface honest against the AUTHORITATIVE source of
display kinds — the producer ``OutboxMessage(kind=...)`` set across ``src`` — NOT
a renderer-file proxy. (The renderer under-reports: producer-only control
sentinels — ``__attach_request__`` / ``__copy_last_reply__`` / ``__rewind_list__``
/ ``__session_switch_request__`` — never appear in ``renderer.py`` yet ARE emitted;
one of them, ``__attach_request__``, is genuinely wire-forwarded. It also
over-reports phantom kinds like ``nodes`` that no producer emits.)

The enumeration reads the codec-input domain directly:

- **(a)** every ``OutboxMessage(kind="literal")`` construction; and
- **(b)** the call sites of *kind-forwarder* functions — a function whose body
  builds ``OutboxMessage(kind=<its own param>)`` is a forwarder (``put_outbox`` /
  ``reply`` / ``_enqueue_tool_call``), so its callers' literal ``kind=`` values
  (and the param's string default) also enter the domain. Forwarders are
  discovered structurally, not hand-listed — so a wrapper-only kind (the
  ``tool_call_*`` trio) can never hide.

Every producer kind must be **dispositioned** as exactly one of:

- **standard-mapped** — the codec emits a standard AG-UI event (``agent`` /
  ``status`` / ``reasoning`` / ``error``); or
- **profiled** — the codec emits a ``CUSTOM`` ``reyn.*`` name that has an
  extension-profile entry; or
- **control-filtered** — the kind is in the explicit
  :data:`~reyn.interfaces.transport.agui.protocol.CONTROL_FILTER_KINDS` allowlist,
  so the emitter never puts it on the wire (a local-control sentinel).

A producer kind outside all three ⇒ RED — an unprofiled Custom name would leak on
the wire, exactly the doc-drift this gate exists to catch. Real instances only —
the real codec + the real profile registry; no mocks.
"""
from __future__ import annotations

import ast
from pathlib import Path

from reyn.core.events.events import Event
from reyn.interfaces.transport.agui.profile import is_profiled, profiled_names
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
from reyn.runtime.outbox import OutboxMessage

_SRC = Path(__file__).resolve().parents[1] / "src"


def _callee_name(func: ast.AST) -> "str | None":
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _kind_kwonly_default(fn: ast.AST) -> "str | None":
    """The string default of a keyword-only ``kind`` param, if any."""
    for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        if arg.arg == "kind" and default is not None and _is_str(default):
            return default.value
    return None


def _src_trees() -> "list[ast.AST]":
    trees: list[ast.AST] = []
    for path in _SRC.rglob("*.py"):
        try:
            trees.append(ast.parse(path.read_text(encoding="utf-8")))
        except SyntaxError:
            continue
    return trees


def _producer_display_kinds() -> set[str]:
    """Every ``kind`` literal that flows into ``OutboxMessage.kind`` across ``src``
    — the AUTHORITATIVE codec-input domain (not the renderer proxy).

    Two literal sources, both followed:

    - **(a)** direct ``OutboxMessage(kind="literal")`` constructions (keyword or
      first positional); and
    - **(b)** call sites of *kind-forwarder* functions — a function that
      constructs ``OutboxMessage(kind=<its param>)`` is a forwarder; its callers'
      literal ``kind=`` values (and the param's string default when ``kind`` is
      omitted) enter the domain. Forwarders are discovered structurally, so a
      wrapper-only kind (``tool_call_*``) cannot hide.

    Kinds passed as a non-literal (a variable) cannot be enumerated and are out of
    scope for a static gate."""
    trees = _src_trees()

    # Pass 1 — discover forwarders (function name → its ``kind`` str default).
    forwarders: dict[str, "str | None"] = {}
    for tree in trees:
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {
                a.arg
                for a in (*fn.args.args, *fn.args.kwonlyargs, *fn.args.posonlyargs)
            }
            forwards = any(
                isinstance(call, ast.Call)
                and _callee_name(call.func) == "OutboxMessage"
                and any(
                    kw.arg == "kind"
                    and isinstance(kw.value, ast.Name)
                    and kw.value.id in params
                    for kw in call.keywords
                )
                for call in ast.walk(fn)
            )
            if forwards:
                forwarders[fn.name] = _kind_kwonly_default(fn)

    # Pass 2 — collect literal kinds from direct constructions + forwarder calls.
    kinds: set[str] = set()
    for tree in trees:
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            name = _callee_name(call.func)
            if name == "OutboxMessage":
                for kw in call.keywords:
                    if kw.arg == "kind" and _is_str(kw.value):
                        kinds.add(kw.value.value)
                if call.args and _is_str(call.args[0]):
                    kinds.add(call.args[0].value)
            elif name in forwarders:
                had_kind = False
                for kw in call.keywords:
                    if kw.arg == "kind":
                        had_kind = True
                        if _is_str(kw.value):
                            kinds.add(kw.value.value)
                if not had_kind and forwarders[name]:
                    kinds.add(forwarders[name])
    return kinds


def _disposition(kind: str) -> str:
    """Classify a producer kind: 'standard' | 'profiled' | 'control' | 'LEAK'."""
    if kind in CONTROL_FILTER_KINDS:
        return "control"
    ev = encode_frame(DisplayFrame(OutboxMessage(kind=kind, text="x")))
    if ev.type != CUSTOM:
        return "standard"
    if is_profiled(ev.data.get("name", "")):
        return "profiled"
    return "LEAK"


def _emitted_custom_names() -> set[str]:
    """The ``reyn.*`` Custom names the codec puts on the wire for the producer
    display-kind domain + the derived chat-event vocabulary — never the profile."""
    names: set[str] = set()
    for kind in _producer_display_kinds():
        if kind in CONTROL_FILTER_KINDS:
            continue  # not forwarded — no wire event to profile
        ev = encode_frame(DisplayFrame(OutboxMessage(kind=kind, text="x")))
        if ev.type == CUSTOM:
            names.add(ev.data["name"])
    for etype in renderer_chat_events():
        ev = encode_frame(EventFrame(Event(type=etype, data={})))
        if ev.type == CUSTOM:
            names.add(ev.data["name"])
    return names


def test_every_producer_kind_is_dispositioned() -> None:
    """Tier 2: every ``OutboxMessage(kind=...)`` producer kind is standard-mapped
    OR profiled OR in the explicit control-filter allowlist. Anything else is an
    unprofiled Custom name that would leak on the wire ⇒ RED.

    Strip-falsify (recorded in the PR): dropping the ``__attach_request__`` profile
    entry, or removing any kind from ``CONTROL_FILTER_KINDS``, makes that kind a
    LEAK ⇒ RED."""
    kinds = _producer_display_kinds()

    # Sanity: the producer scan found the real domain — including the
    # producer-ONLY control sentinels (invisible to any renderer scan) and the
    # wrapper-forwarded ``tool_call_*`` trio (invisible to a direct-constructor
    # scan). A broken scan that found nothing must not vacuously pass.
    assert {
        "agent", "status", "reasoning", "error",          # standard-mapped
        "system", "user", "__attach_request__",           # profiled CUSTOM
        "tool_call_started",                              # wrapper-forwarded
        "__end__", "__copy_last_reply__",                 # control-filtered
    } <= kinds

    leaks = {k for k in kinds if _disposition(k) == "LEAK"}
    assert not leaks, (
        "producer kinds with no disposition (standard-map, profile, or add to "
        f"CONTROL_FILTER_KINDS): {sorted(leaks)}"
    )


def test_control_sentinel_dispositions_client_consumed_forward_upstream_consumed_filter() -> None:
    """Tier 2: the per-entry disposition of the `__…__` control sentinels.

    - ``__copy_last_reply__`` / ``__rewind_list__`` are **client-consumed** over the
      transport stream (real client-side clipboard copy / rewind picker), so they
      are FORWARDED (profiled CUSTOM) and round-trip losslessly — filtering them
      would make remote ``/copy`` / ``/rewind`` silent no-ops.
    - ``__end__`` (terminal) and ``__session_switch_request__`` (upstream-consumed
      at registry.py:3061) are control-filtered.
    - ``__attach_request__`` is upstream-consumed at registry.py:3052; its profile
      entry is a fail-safe (not a live wire kind)."""
    # Client-consumed → forwarded + profiled + lossless round-trip.
    for client_kind in ("__copy_last_reply__", "__rewind_list__"):
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
    """Tier 2: each reyn.* Custom name the codec emits (for a forwarded producer
    kind) has an extension-profile entry. An unprofiled Custom name ⇒ RED."""
    emitted = _emitted_custom_names()

    # Sanity: the enumeration found the real emitted Custom vocabulary, including
    # the producer-only ``__attach_request__`` (invisible to a renderer scan) and
    # the wrapper-forwarded ``tool_call_started`` (invisible to a direct-ctor scan).
    assert {
        "reyn.display.system",
        "reyn.display.__attach_request__",
        "reyn.display.tool_call_started",
        "reyn.event.user_answered_intervention",
    } <= emitted

    missing = {name for name in emitted if not is_profiled(name)}
    assert not missing, f"unprofiled reyn.* Custom names (add to profile): {sorted(missing)}"


def test_intervention_frontend_tool_toolname_is_profiled() -> None:
    """Tier 2: the HITL frontend-tool ``toolName`` the emitter really produces
    falls under a profiled reyn.intervention.* namespace — for every intervention
    kind (open namespace). Unprofiled ⇒ RED (the P3 members were the stale-base gap)."""
    # Real emitter output for representative intervention kinds (ask_user free-text
    # + a permission.* prompt) — enumerated from the codec, not the profile.
    for kind in ("ask_user", "permission.grant_deny"):
        ev = encode_intervention_tool_start(
            {"intervention_id": "iv-1", "intervention_kind": kind, "prompt": "?"}
        )
        toolname = ev.data["toolName"]
        assert toolname.startswith("reyn.intervention."), toolname
        assert is_profiled(toolname), f"unprofiled intervention frontend-tool: {toolname}"

    # The empty-kind fallback (``reyn.intervention.ask_user``) is also profiled.
    ev = encode_intervention_tool_start({"intervention_id": "iv-2", "intervention_kind": ""})
    assert is_profiled(ev.data["toolName"])


def test_reasoning_is_standard_mapped_not_a_custom_profile_entry() -> None:
    """Tier 2: P6a — the reasoning frame is STANDARD-mapped (a Reasoning* event),
    so it needs NO reyn.* Custom profile entry. The completeness gate must expect
    reasoning as standard-mapped, not demand a ``reyn.display.reasoning`` Custom
    entry for a now-standard signal.

    Strip-falsify: remove ``"reasoning"`` from ``_DISPLAY_KIND_EVENT`` → the frame
    reverts to CUSTOM (``reyn.display.reasoning``) → the standard-mapped assertion
    below goes RED (and the general completeness gate would then demand a Custom
    entry that no longer exists)."""
    ev = encode_frame(DisplayFrame(OutboxMessage(kind="reasoning", text="thinking")))

    # Standard-mapped, not CUSTOM: no reyn.* Custom name is emitted for reasoning.
    assert ev.type == REASONING_MESSAGE_CONTENT
    assert ev.type != CUSTOM
    assert "name" not in ev.data, "a standard Reasoning* event carries no CUSTOM name"

    # The move's consequence: there is NO Custom profile entry for reasoning, and
    # the completeness gate does not require one (a standard event needs no
    # extension-profile entry).
    assert "reyn.display.reasoning" not in profiled_names()
    assert "reyn.display.reasoning" not in _emitted_custom_names()
