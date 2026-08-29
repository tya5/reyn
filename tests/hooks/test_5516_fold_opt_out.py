"""Tier 1/2: #5516 §1/§1b — the per-hook ``fold:`` opt-out flag.

Reopened finding (lead-coder, #5516 issue thread, 2026-08-29): the
canonical spec's own §1 table has 4 rows — array/skipped/back-compat were
implemented and verified, but the opt-out flag itself was never added
(``_KNOWN_HOOK_ENTRY_KEYS`` had no ``fold`` key, and no dispatch-level
code branched on it). This file closes that gap, with BOTH acceptance
directions the reopened issue explicitly requires: default (no flag, or
``fold: true``) still folds; ``fold: false`` makes a hook receive N
SEPARATE single-item-array launches instead of one N-item launch.

Real ``load_hooks``/``HookDispatcher``/``HookRegistry`` — recording
async callables for the injected seams (the established DI shape this
module's other tests use), no mocks."""
from __future__ import annotations

import pytest

from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.loader import HookConfigError, load_hooks
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef, PushBlock


class _Recorder:
    """A real recording async callable — captures (args, kwargs) per call,
    no mock (mirrors test_hook_dispatcher_1800_5b.py's own helper)."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


def _make_dispatcher(hooks: list[HookDef], **seams) -> tuple[HookDispatcher, dict]:
    seams.setdefault("put_inbox", _Recorder())
    seams.setdefault("stage_next_turn_context", _Recorder())
    seams.setdefault("run_shell", _Recorder())
    disp = HookDispatcher(
        HookRegistry(hooks),
        put_inbox=seams["put_inbox"],
        stage_next_turn_context=seams["stage_next_turn_context"],
        run_shell=seams["run_shell"],
    )
    return disp, seams


def _load_one(raw_hook: dict) -> HookDef:
    reg = load_hooks([raw_hook])
    (hook,) = reg.hooks_for(raw_hook["on"])
    return hook


# ---------------------------------------------------------------------------
# Loader contract (Tier 1)
# ---------------------------------------------------------------------------


def test_fold_true_parses_as_operator_will():
    """Tier 1: ``fold: true`` parses to True — the operator's explicit
    (redundant with the default, but expressible) will."""
    hook = _load_one({"on": "turn_end", "exec": ["echo", "hi"], "fold": True})
    assert hook.fold is True


def test_fold_false_parses_as_explicit_opt_out():
    """Tier 1: ``fold: false`` parses to False — DISTINCT from omitted
    (None). The explicit-vs-omitted distinction is the #2964 principle
    every other per-hook knob in this module hinges on."""
    hook = _load_one({"on": "turn_end", "exec": ["echo", "hi"], "fold": False})
    assert hook.fold is False


def test_fold_omitted_is_none_not_true():
    """Tier 1: omitting the key yields None (= the floor = fold), NOT a
    bare True — so "operator said nothing" stays distinguishable from
    "operator explicitly confirmed folding"."""
    hook = _load_one({"on": "turn_end", "exec": ["echo", "hi"]})
    assert hook.fold is None


def test_fold_rejected_on_pipeline_launch():
    """Tier 1: eager-rejection — ``fold:`` on a pipeline_launch hook is a
    config ERROR, not a silent ignore. pipeline_launch's receiver takes
    ONE input: dict and can never fold at all (architect ruling, #5516);
    a silently-ignored operator flag here would read as an applied
    choice that was never applied (the #2976 model every other per-hook
    knob in this module already follows)."""
    with pytest.raises(HookConfigError, match="fold"):
        load_hooks([{
            "on": "mcp_resource_updated",
            "pipeline_launch": {"name": "reindex", "input_template": {"uri": "{{ uri }}"}},
            "fold": False,
        }])


def test_fold_rejected_when_not_a_boolean():
    """Tier 1: a non-bool ``fold:`` is a config error — a truthy string
    like "false" must never be silently coerced."""
    with pytest.raises(HookConfigError, match="fold"):
        load_hooks([{"on": "turn_end", "exec": ["echo", "hi"], "fold": "false"}])


def test_fold_accepted_on_template_push():
    """Tier 1: ``fold:`` is valid on template_push (one of the 3 schemes
    that CAN fold — text concatenation), not just exec/exec_capture."""
    hook = _load_one({
        "on": "turn_end",
        "template_push": {"message": "hi", "wake": False},
        "fold": False,
    })
    assert hook.fold is False


# ---------------------------------------------------------------------------
# Both acceptance directions (Tier 2), driven through the real dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_no_flag_still_folds_into_one_launch():
    """Tier 2: acceptance direction ① — no ``fold:`` key at all (the
    floor) still folds N events into ONE launch, unchanged from #5516's
    own base implementation."""
    hook = HookDef(on="mcp_resource_updated", exec=("echo", "hi"))
    disp, seams = _make_dispatcher([hook])

    payloads = [
        {"uri": f"file:///{i}", "point": "mcp_resource_updated"} for i in range(3)
    ]
    await disp.dispatch_external_batch("mcp_resource_updated", payloads)

    (call,) = seams["run_shell"].calls
    (args, _kwargs) = call
    event_context = args[1]
    assert {e["uri"] for e in event_context["events"]} == {
        f"file:///{i}" for i in range(3)
    }


@pytest.mark.asyncio
async def test_explicit_fold_true_also_folds_into_one_launch():
    """Tier 2: ``fold: true`` (the operator's explicit, redundant-with-
    default confirmation) behaves identically to omitting the key."""
    hook = HookDef(on="mcp_resource_updated", exec=("echo", "hi"), fold=True)
    disp, seams = _make_dispatcher([hook])

    payloads = [
        {"uri": f"file:///{i}", "point": "mcp_resource_updated"} for i in range(3)
    ]
    await disp.dispatch_external_batch("mcp_resource_updated", payloads)

    (call,) = seams["run_shell"].calls
    (args, _kwargs) = call
    event_context = args[1]
    assert {e["uri"] for e in event_context["events"]} == {
        f"file:///{i}" for i in range(3)
    }


@pytest.mark.asyncio
async def test_fold_false_gives_n_separate_single_item_launches():
    """Tier 2: LOAD-BEARING — acceptance direction ② (the one the
    reopened issue named explicitly). ``fold: false`` makes an opted-out
    hook receive its matched events as N SEPARATE launches, each
    array-wrapped as a single-item ``[payload]`` (the #5516 clean-break
    stays unconditional — only the LAUNCH COUNT differs, never the
    array-wrapping itself)."""
    hook = HookDef(on="mcp_resource_updated", exec=("echo", "hi"), fold=False)
    disp, seams = _make_dispatcher([hook])

    event_count = 3
    payloads = [
        {"uri": f"file:///{i}", "point": "mcp_resource_updated"} for i in range(event_count)
    ]
    await disp.dispatch_external_batch("mcp_resource_updated", payloads)

    assert len(seams["run_shell"].calls) == event_count, (
        f"an opted-out hook must get ONE launch PER EVENT, not one folded "
        f"launch -- got {len(seams['run_shell'].calls)}"
    )
    seen_uris = set()
    for args, _kwargs in seams["run_shell"].calls:
        event_context = args[1]
        (only_event,) = event_context["events"]  # each launch: exactly 1 item
        seen_uris.add(only_event["uri"])
    assert seen_uris == {f"file:///{i}" for i in range(event_count)}, (
        "every event's data must survive across the N separate launches"
    )


@pytest.mark.asyncio
async def test_fold_false_skipped_session_wide_still_applies_once():
    """Tier 2: owner §1b item ② — skipped_session_wide applies
    REGARDLESS of the fold flag (an event lost to bridge queue overflow
    is lost either way; folding only decides what happens to events that
    made it INTO the queue). Reported on the FIRST of the N opted-out
    launches only, not duplicated across all N (it is a session-wide
    count, not a per-event one)."""
    hook = HookDef(on="mcp_resource_updated", exec=("echo", "hi"), fold=False)
    disp, seams = _make_dispatcher([hook])

    payloads = [
        {"uri": f"file:///{i}", "point": "mcp_resource_updated"} for i in range(3)
    ]
    await disp.dispatch_external_batch(
        "mcp_resource_updated", payloads, skipped_session_wide=5,
    )

    reported = [
        args[1]["skipped_session_wide"] for args, _kwargs in seams["run_shell"].calls
    ]
    assert reported.count(5) == 1, (
        f"skipped_session_wide=5 must be reported EXACTLY ONCE across the "
        f"N opted-out launches (not zero, not duplicated N times) -- got "
        f"{reported!r}"
    )
    assert all(v in (0, 5) for v in reported)


@pytest.mark.asyncio
async def test_fold_false_template_push_also_launches_n_times():
    """Tier 2: the opt-out applies to template_push too (not just exec)
    — N separate pushes, never concatenated, when opted out."""
    hook = HookDef(
        on="mcp_resource_updated",
        template_push=PushBlock(message="uri={{ uri }}", wake=True),
        fold=False,
    )
    disp, seams = _make_dispatcher([hook])

    event_count = 3
    payloads = [
        {"uri": f"file:///{i}", "point": "mcp_resource_updated"} for i in range(event_count)
    ]
    await disp.dispatch_external_batch("mcp_resource_updated", payloads)

    assert len(seams["put_inbox"].calls) == event_count, (
        f"an opted-out template_push hook must fire ONE push PER EVENT -- "
        f"got {len(seams['put_inbox'].calls)}"
    )
    texts = [args[1]["text"] for args, _kwargs in seams["put_inbox"].calls]
    for i in range(event_count):
        assert any(f"uri=file:///{i}" == t for t in texts), (
            f"event {i}'s own, UNconcatenated push missing -- got {texts!r}"
        )
