"""Tier 2: #6184 段3-1 — the subject-params-exist-in-own-schema gate.

Real ``get_default_registry()`` throughout, except where a test
deliberately constructs a synthetic registry (a small list of real
``ToolDefinition`` instances, never a mock) to exercise a specific diff
shape — matching ``test_check_hooks_declared_reachable_5190.py``'s own
pattern of real-registry-then-synthetic-fixture.
"""
from __future__ import annotations

import os
import subprocess
import sys

from reyn.tools import get_default_registry
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult
from scripts.check_subject_params_exist_in_own_schema import find_stale_subject_params
from tests._support.paths import REPO_ROOT


async def _noop_handler(args, ctx: ToolContext) -> ToolResult:  # pragma: no cover - never invoked
    return {"status": "ok"}


def _tool(name: str, *, properties: dict, subject_params: tuple = ()) -> ToolDefinition:
    """A minimal, real ``ToolDefinition`` — never a mock, per CLAUDE.md's
    own ban. Only the fields this gate reads (``name``/``parameters``/
    ``subject_params``) are given real content; the rest take the
    dataclass's own defaults, which is a legitimate real instance the
    same way every other test in this suite constructs one."""
    return ToolDefinition(
        name=name,
        description="",
        parameters={"type": "object", "properties": properties},
        gates=ToolGates(),
        handler=_noop_handler,
        category="io",
        subject_params=subject_params,
    )


# ── acceptance① — purely additive: the real registry's diff is empty today ──


def test_the_real_registrys_diff_is_currently_empty() -> None:
    """Tier 2: acceptance① — 段3-1 declares the field but declares no
    tool's subject_params yet (accept④, owner-judgment-gated for a later
    stage); this gate's own starting population is zero, so any hit here
    is a real regression, not inherited debt."""
    offenders = find_stale_subject_params()
    assert offenders == [], (
        f"real regression(s) found: {offenders} — a tool declared a "
        "subject_params name that does not exist in its own schema"
    )


def test_every_tool_in_the_real_registry_defaults_to_empty_subject_params() -> None:
    """Tier 2: non-vacuity for① — pins the field is genuinely additive
    (every tool but ``exec`` has the default ``()``), not merely that
    the gate's own diff happens to be empty for some other reason (e.g.
    every tool's ``parameters`` being unreadable).

    #6184 CI fix (lead-coder, measured — #6200's own CI, own review
    admission: "空虚かは見たが、次の段がこれを壊すかを問わなかった"):
    this test's own original claim ("every tool" with no exception) was
    correct at 段3-1's own merge but 段3-2 (a LATER stage, same arc)
    intentionally makes it false for ``exec`` — the SAME "merge-time
    observation, not a standing invariant" shape #6190/#6191's own
    tests already disclosed for their own claims, which this one did
    not. ``exec`` is now excluded by NAME rather than the claim being
    weakened to "at least one" — kept this precise so a SECOND tool
    gaining a declaration without a matching update here still fails.
    See ``tests/runtime/test_6184_stage3_2_producer_fills_subject.py``'s
    own ``test_every_currently_registered_tool_except_exec_has_no_
    subject_params`` for that stage's own, differently-scoped version
    of this same underlying fact (kept as two tests, not merged: this
    one is about the GATE's own non-vacuity, that one is about 段3-2's
    own accept criteria — different questions that currently share an
    answer)."""
    registry = get_default_registry()
    tools = list(registry)
    assert tools, "setup: the real registry must not be empty"
    assert all(t.subject_params == () for t in tools if t.name != "exec")


# ── acceptance② — a stale subject_params name is flagged, a valid one is not ──


def test_a_subject_params_name_absent_from_the_tools_own_schema_is_flagged() -> None:
    """Tier 2: acceptance② — a tool declaring ``subject_params=("ghost",)``
    with no ``"ghost"`` key in its own ``parameters["properties"]`` must
    appear in the diff — the exact "param renamed, pointer left stale"
    shape this gate exists to catch."""
    registry = [_tool("t1", properties={"path": {"type": "string"}}, subject_params=("ghost",))]
    offenders = find_stale_subject_params(registry=registry)
    assert offenders == [("t1", "ghost")]


def test_a_subject_params_name_present_in_the_tools_own_schema_is_not_flagged() -> None:
    """Tier 2: accept-side of② — a subject_params name that DOES exist in
    the same tool's own properties clears the gate, proving this is a
    genuine per-tool cross-check and not an unconditional "any non-empty
    subject_params fails" trip-wire."""
    registry = [_tool("t1", properties={"path": {"type": "string"}}, subject_params=("path",))]
    offenders = find_stale_subject_params(registry=registry)
    assert offenders == []


def test_a_valid_and_a_stale_name_on_the_same_tool_flags_only_the_stale_one() -> None:
    """Tier 2: accept-side② + non-vacuity — a tool with BOTH a valid and a
    stale subject_params entry proves the check inspects each declared
    name independently, not "does this tool have ANY valid pointer"."""
    registry = [
        _tool(
            "t1", properties={"path": {"type": "string"}},
            subject_params=("path", "ghost"),
        ),
    ]
    offenders = find_stale_subject_params(registry=registry)
    assert offenders == [("t1", "ghost")]


def test_a_tool_with_no_properties_key_at_all_flags_every_declared_name() -> None:
    """Tier 2: edge case — a schema with no ``"properties"`` key at all
    (not merely an empty one) must not raise, and every declared
    subject_params name is correctly flagged against the empty effective
    property set."""
    registry = [
        ToolDefinition(
            name="t1", description="", parameters={"type": "object"},
            gates=ToolGates(), handler=_noop_handler, category="io",
            subject_params=("anything",),
        ),
    ]
    offenders = find_stale_subject_params(registry=registry)
    assert offenders == [("t1", "anything")]


# ── acceptance③ — HISTORICAL: 0 read sites in src/ (段3-2 landed 2 real readers) ──


def test_subject_params_readers_are_the_closed_producer_set_6184_stage3_2_added() -> None:
    """Tier 2: acceptance③, superseded (lead-coder, measured — #6200's
    own CI, own review admission: "空虚かは見たが、次の段がこれを壊すか
    を問わなかった"). "0 readers" was TRUE at 段3-1's own merge but is
    now PERMANENTLY false — #6184 段3-2 (a LATER stage, same arc) added
    the field's first real consumer on purpose (that IS 段3-2's own
    job), the SAME "merge-time observation, not a standing invariant"
    shape #6190/#6191's own tests already disclosed for their own
    claims, which this one did not.

    Re-scoped from "0 readers" to "readers are exactly the 2 files
    段3-2's own producer path added" — still a real regression witness
    (a THIRD, unexpected reader still fails this), not merely deleted.
    Caveat, disclosed rather than hidden: this is a substring grep for
    the literal text ``.subject_params`` — it also matches a PROSE
    mention inside a comment/docstring (as ``lifecycle_forwarder.py``'s
    own reference does — a comment naming the field, not a code read of
    it), so the pinned set below is not a precise "who reads the field
    at runtime" answer, only "which files currently contain that
    substring anywhere". Good enough to catch an unexpected NEW file
    joining the set; not proof against a comment added to an EXISTING
    already-listed file."""
    src_root = REPO_ROOT / "src"
    hits: "list[str]" = []
    for path in src_root.rglob("*.py"):
        if path.name in ("types.py",):
            # types.py:ToolDefinition's own field declaration is the
            # subject of this gate, not a "read" of it.
            continue
        text = path.read_text(encoding="utf-8")
        if ".subject_params" in text:
            hits.append(str(path.relative_to(REPO_ROOT)))
    expected = {
        "src/reyn/tools/subject.py",
        "src/reyn/runtime/lifecycle_forwarder.py",
    }
    assert set(hits) == expected, (
        f"reader set changed since #6184 段3-2 landed its own 2 -- "
        f"got {hits}, expected {sorted(expected)}"
    )


# ── the gate script actually runs cleanly as a subprocess ────────────────


def test_the_gate_script_runs_clean_as_a_real_subprocess(out_of_process_reyn: str) -> None:
    """Tier 2: the same "run it before shipping it" discipline the
    hooks-declared-reachable gate's own test file uses — a real
    subprocess invocation, since that's how CI actually exercises this
    script. Pins ``out_of_process_reyn``'s own src root as the spawn's
    ``PYTHONPATH`` rather than re-deriving it from ``REPO_ROOT`` (#3024:
    the ambient venv, not this test file, decides which checkout a bare
    spawn resolves ``reyn`` from)."""
    gate_script = REPO_ROOT / "scripts" / "check_subject_params_exist_in_own_schema.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = out_of_process_reyn
    result = subprocess.run(
        [sys.executable, str(gate_script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"gate script failed as a real subprocess: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout
