"""Tier 2: #5190 — the hooks declared-vs-reachable-without-an-LLM-turn
gate.

Real registries throughout — the real ``BUILTIN_HOOK_SCHEMAS`` and the
real ``REACHABLE_WITHOUT_LLM_TURN`` (except where a test deliberately
constructs a synthetic dict to exercise a specific diff shape, matching
the ``check_approval_ledger_import_boundary`` test file's own pattern of
real-file-then-synthetic-fixture).
"""
from __future__ import annotations

import os
import subprocess
import sys

from reyn.hooks.schema_registry import BUILTIN_HOOK_SCHEMAS
from scripts.check_hooks_declared_reachable import (
    REACHABLE_WITHOUT_LLM_TURN,
    find_undeclared_reachability,
)
from tests._support.paths import REPO_ROOT

# ── acceptance① — the real registry, right now, has an empty diff ────────


def test_the_real_registries_diff_is_currently_empty() -> None:
    """Tier 2: acceptance① — landed on main, every declarable kind has a
    registered reachability witness. This gate's own starting population
    is zero, so any hit here is a new regression (a schema addition that
    forgot its citation), not inherited debt."""
    offenders = find_undeclared_reachability()
    assert offenders == [], (
        f"real regression(s) found: {offenders} — a kind was added to "
        "BUILTIN_HOOK_SCHEMAS with no matching REACHABLE_WITHOUT_LLM_TURN "
        "citation"
    )


def test_every_declarable_kind_is_covered_one_to_one() -> None:
    """Tier 2: non-vacuity for① — pins that the real registry is not
    merely a superset that happens to swallow the diff (e.g. stale
    entries for kinds no longer declarable masking a real gap); asserts
    the two key-sets are IDENTICAL, not just that the subtraction above
    is empty by coincidence of one side being huge."""
    assert set(REACHABLE_WITHOUT_LLM_TURN) == set(BUILTIN_HOOK_SCHEMAS)


# ── acceptance② — a new schema kind with no citation goes red ────────────


def test_a_new_schema_kind_with_no_citation_is_flagged() -> None:
    """Tier 2: acceptance② — the exact #5167 shape reproduced
    synthetically: a hypothetical 10th declarable kind
    (`builtin:external:new_thing`) with NO matching registry entry must
    appear in the diff. Without this check, a future PR could add a
    schema entry and this gate would stay silently green ("empty today"
    read as "always empty"), the architect's own named risk
    (issuecomment-5384661356: "これが無いと『今空だから緑』")."""
    schemas = dict(BUILTIN_HOOK_SCHEMAS)
    schemas["builtin:external:new_thing"] = frozenset({"point"})
    offenders = find_undeclared_reachability(schemas=schemas)
    assert offenders == ["builtin:external:new_thing"]


def test_a_new_schema_kind_with_a_citation_is_not_flagged() -> None:
    """Tier 2: the accept-side of② — adding BOTH the schema entry and its
    citation in the same change clears the gate, proving the check is a
    genuine set diff and not an unconditional "any change to schemas
    fails" trip-wire."""
    schemas = dict(BUILTIN_HOOK_SCHEMAS)
    schemas["builtin:external:new_thing"] = frozenset({"point"})
    registry = dict(REACHABLE_WITHOUT_LLM_TURN)
    registry["builtin:external:new_thing"] = "some/real/path.py:1 — real witness"
    offenders = find_undeclared_reachability(schemas=schemas, registry=registry)
    assert offenders == []


# ── acceptance③ — an LLM-tool-only producer must not count as reachable ──


def test_task_settled_citation_is_not_the_llm_tool_only_producer() -> None:
    """Tier 2: acceptance③, the #5167-trap witness — `task_settled` has
    TWO real producers (inter_agent_messaging.py's run_prompt(collect=
    "async") settle branch, LLM-tool-invoked; pipeline_executor_driver.py's
    Pipeline-settle branch, config-driven). The registered citation must
    point at the config-driven one, NOT the LLM-tool-invoked one — citing
    the wrong producer here would silently reproduce #5167's own defect
    one kind later, just with the gate reporting green over it."""
    citation = REACHABLE_WITHOUT_LLM_TURN["builtin:task:task_settled"]
    assert citation.startswith("runtime/services/pipeline_executor_driver.py"), (
        f"task_settled's citation must be ANCHORED on the config-driven "
        f"pipeline settle path (not merely mention it in passing), got: "
        f"{citation!r}"
    )
    assert 'run_prompt(collect="async")' in citation and "LLM-tool-invoked" in citation, (
        "the citation must explicitly name and reject the LLM-tool-gated "
        f"producer, not merely omit it — got: {citation!r}"
    )


def test_a_citation_naming_only_an_llm_gated_path_would_not_be_caught_mechanically() -> None:
    """Tier 2: disclosed limitation, not a defect — this gate's registry
    is HAND-maintained (mirrors DYNAMIC_KIND_EMIT_SITES's own trust
    boundary, see the gate module's own docstring); it cannot mechanically
    verify a citation's PROSE is truthful, only that a citation EXISTS.
    A reviewer misreading an LLM-gated call site as a valid witness would
    still pass this gate — the previous test is what actually holds
    task_settled's citation honest, not this mechanism. Documented here
    so the boundary is explicit, not implied."""
    registry = {"builtin:task:task_settled": "a plausible-looking but wrong citation"}
    offenders = find_undeclared_reachability(
        schemas={"builtin:task:task_settled": frozenset()}, registry=registry,
    )
    assert offenders == [], (
        "the mechanism only checks presence, not truthfulness — by design, "
        "see this test's own docstring"
    )


# ── acceptance④ — config-dependent reachability is NOT a false positive ──


def test_config_dependent_kinds_have_registered_witnesses() -> None:
    """Tier 2: acceptance④ — file_changed/cron_fired/webhook_received/
    mcp_resource_updated only actually FIRE under specific operator
    configuration (fs_watch.paths set, a cron job scheduled, a webhook
    configured, a concrete-matcher hook declared) — but the CODE PATH is
    unconditional, so all 4 must be present in the registry (the gate
    must not treat "config-gated" as equivalent to "LLM-gated")."""
    config_dependent = {
        "builtin:external:file_changed",
        "builtin:external:cron_fired",
        "builtin:external:webhook_received",
        "builtin:external:mcp_resource_updated",
    }
    assert config_dependent <= set(REACHABLE_WITHOUT_LLM_TURN)
    # Non-vacuity: confirm these are real declarable kinds too, not a
    # set this test invented independently of the schema.
    assert config_dependent <= set(BUILTIN_HOOK_SCHEMAS)


# ── acceptance⑤ — the failure message names exactly 2 remedies, never an allowlist ──


def test_failure_message_names_exactly_two_remedies_never_an_allowlist() -> None:
    """Tier 2: acceptance⑤ — architect ruling (issuecomment-5384661356):
    the only 2 legitimate repairs are (a) build reachability or (b) drop
    the kind from the declarable schema; an allowlist/exemption must
    never be offered as a way out. Drives the real `main()` with a
    monkeypatched schema (a synthetic gap) and reads the real printed
    message."""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    import scripts.check_hooks_declared_reachable as gate_module

    original = gate_module.BUILTIN_HOOK_SCHEMAS
    try:
        gate_module.BUILTIN_HOOK_SCHEMAS = dict(original) | {
            "builtin:external:new_thing": frozenset({"point"}),
        }
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gate_module.main()
    finally:
        gate_module.BUILTIN_HOOK_SCHEMAS = original

    assert code == 1
    message = err.getvalue()
    assert "builtin:external:new_thing" in message
    assert "(a)" in message and "(b)" in message, (
        f"expected exactly 2 lettered remedies in the failure message, "
        f"got: {message!r}"
    )
    assert "(c)" not in message
    assert "allowlist" in message.lower(), (
        "the message must explicitly name and reject allowlist as a "
        "non-option, not merely omit it"
    )
    assert "not a third option" in message or "not an option" in message.lower() or (
        "allowlist" in message.lower() and "not" in message.lower()
    )


def test_a_clean_registry_reports_ok_and_exits_zero() -> None:
    """Tier 2: accept-side of⑤ — the real, unmodified registries print an
    OK line and exit 0, driven through the real `main()`."""
    import io
    from contextlib import redirect_stdout

    from scripts.check_hooks_declared_reachable import main

    out = io.StringIO()
    with redirect_stdout(out):
        code = main()
    assert code == 0
    assert "OK" in out.getvalue()


# ── the gate script actually runs cleanly as a subprocess ────────────────


def test_the_gate_script_runs_clean_as_a_real_subprocess() -> None:
    """Tier 2: the same "run it before shipping it" discipline the
    approval-ledger/TUI-widget boundary gates use — a real subprocess
    invocation, not just an in-process function call, since that's how
    CI actually exercises this script."""
    gate_script = REPO_ROOT / "scripts" / "check_hooks_declared_reachable.py"
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
