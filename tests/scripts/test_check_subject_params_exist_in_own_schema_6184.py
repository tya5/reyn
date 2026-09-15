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
    (every one of today's 79 tools has the default ``()``), not merely
    that the gate's own diff happens to be empty for some other reason
    (e.g. every tool's ``parameters`` being unreadable)."""
    registry = get_default_registry()
    tools = list(registry)
    assert tools, "setup: the real registry must not be empty"
    assert all(t.subject_params == () for t in tools)


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


# ── acceptance③ — 0 read sites in src/ (the field is not yet consumed) ──


def test_no_src_file_reads_subject_params_yet() -> None:
    """Tier 2: acceptance③ — grep witness that nothing under ``src/``
    reads ``.subject_params`` yet (段3's eventual consumer is a LATER
    stage, not this one). This file's own construction of the field
    (``ToolDefinition(..., subject_params=...)``) and the gate script's
    own read are excluded by design (they are what accept③ says should
    NOT exist elsewhere, not the declaration site itself)."""
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
    assert hits == [], (
        f"src/ file(s) already read .subject_params, but 段3-1's own "
        f"accept③ says nothing should yet: {hits}"
    )


# ── the gate script actually runs cleanly as a subprocess ────────────────


def test_the_gate_script_runs_clean_as_a_real_subprocess() -> None:
    """Tier 2: the same "run it before shipping it" discipline the
    hooks-declared-reachable gate's own test file uses — a real
    subprocess invocation, since that's how CI actually exercises this
    script."""
    gate_script = REPO_ROOT / "scripts" / "check_subject_params_exist_in_own_schema.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
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
