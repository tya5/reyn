"""Tier 2: #5714 (architect ruling) — the process marker's own identity
field was the wrong SHAPE for the fact it recorded. ``agent_name``/
``broker_session_id`` are SESSION facts, but #5709 R9 placed them as a
SINGULAR pair on the PROCESS marker; #5694 confirmed 1 process : N
Session (``AgentRegistry.ensure_running`` runs every agent's own
``session.run()`` as a concurrent asyncio task in the SAME process,
never a separate OS process per agent). The second Session constructed
in one process silently overwrote the first's identity —
``process_for_agent(first_agent)`` then returned ``[]`` while that
Session was genuinely still alive. e2e-coder reproduced this directly
(#5714's own issue thread) before this fix existed; this file's own
accept tests are that SAME reproduction, now expected GREEN.

Fix: the field is a COLLECTION (``sessions``), keyed by
``(agent_name, sid)`` — same key overwrites its own entry (bounded:
"how many DISTINCT keys this process has ever hosted"). Each entry
gains an ``ended_at``, pressed from the SAME #5694 done-callback
(``AgentRegistry._on_session_run_task_done``) — not a second lifecycle
mechanism.

Real ``AgentRegistry`` + real ``Session`` throughout for the
multi-session scenarios (mirrors #5694's own test file's own
``_registry`` helper) — no mocks.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from reyn.runtime import process_registry
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session
from tests._support.paths import REPO_ROOT


@pytest.fixture(autouse=True)
def _isolated_processes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Mirrors test_5226_process_registry.py's own fixture — the real
    ``~/.reyn/processes/`` must never be touched by a test."""
    processes_dir = tmp_path / "processes"
    monkeypatch.setattr(process_registry, "PROCESSES_DIR", processes_dir)
    return processes_dir


def _registry(tmp_path: Path) -> AgentRegistry:
    shared = BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=shared,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    return AgentRegistry(project_root=tmp_path, session_factory=factory)


def _marker(processes_dir: Path, pid: int) -> dict:
    return json.loads((processes_dir / f"{pid}.json").read_text(encoding="utf-8"))


# ── the reproduced incident itself, now expected green ─────────────────────


def test_two_sessions_in_one_process_both_appear_in_the_marker(
    tmp_path: Path, _isolated_processes_dir: Path,
) -> None:
    """Tier 2: #5714's own accept ② — e2e-coder's reproduction, same
    construction, now green. Both agents' identities are readable from
    ONE process's marker at once — the pre-fix defect was the SECOND
    Session's construction silently erasing the FIRST's."""
    import os

    process_registry.register_process("chat")
    try:
        reg = _registry(tmp_path)
        reg.get_or_load("default")
        reg.create("coder-brown")
        reg.get_or_load("coder-brown")

        marker = _marker(_isolated_processes_dir, os.getpid())
        names = {e["agent_name"] for e in marker["sessions"]}
        assert names == {"default", "coder-brown"}, (
            f"#5714 REGRESSION: expected both agents' identities recorded "
            f"in ONE process marker, got {names!r}"
        )

        assert process_registry.process_for_agent("default") != [], (
            "#5714 REGRESSION: process_for_agent('default') returned [] "
            "while the 'default' Session was still alive — the exact "
            "incident this issue reproduces"
        )
        assert process_registry.process_for_agent("coder-brown") != []
    finally:
        process_registry.unregister_process(os.getpid())


def test_process_for_agent_returns_only_the_matching_agents_process(
    tmp_path: Path, _isolated_processes_dir: Path,
) -> None:
    """Tier 2: #5714 accept ⑦ — among 2 co-hosted sessions, a query for
    ONE of them must not spuriously imply anything about the other by
    virtue of sharing a process; the returned marker is correct, and a
    query for a name never recorded anywhere returns []."""
    import os

    process_registry.register_process("chat")
    try:
        reg = _registry(tmp_path)
        reg.get_or_load("default")
        reg.create("coder-brown")
        reg.get_or_load("coder-brown")

        matched = process_registry.process_for_agent("default")
        assert {m["pid"] for m in matched} == {os.getpid()}
        assert process_registry.process_for_agent("nonexistent-agent") == []
    finally:
        process_registry.unregister_process(os.getpid())


# ── same-key overwrite, never a duplicate ───────────────────────────────────


def test_recording_the_same_agent_name_and_sid_twice_does_not_duplicate(
    _isolated_processes_dir: Path,
) -> None:
    """Tier 2: #5714 accept ③ — "同じ key の session が作り直されたら
    上書き". A Session genuinely rebuilt under the SAME (agent_name, sid)
    reuses its own entry rather than accumulating a second one."""
    import os

    process_registry.register_process("chat")
    try:
        process_registry.record_process_identity(agent_name="default", sid="main")
        process_registry.record_process_identity(
            agent_name="default", sid="main", broker_session_id="b1",
        )
        marker = _marker(_isolated_processes_dir, os.getpid())
        try:
            [entry] = marker["sessions"]
        except ValueError:
            raise AssertionError(
                f"#5714 REGRESSION: recording the same (agent_name, sid) "
                f"twice must overwrite in place, not append — got "
                f"{marker['sessions']!r}"
            ) from None
        assert entry["broker_session_id"] == "b1", (
            "the second call's broker_session_id must have applied to "
            "the SAME entry"
        )
    finally:
        process_registry.unregister_process(os.getpid())


def test_two_distinct_sids_for_the_same_agent_are_two_entries(
    _isolated_processes_dir: Path,
) -> None:
    """Tier 2: the key is (agent_name, sid) — NOT agent_name alone. Two
    genuinely different sessions of the same agent are two entries."""
    import os

    process_registry.register_process("chat")
    try:
        process_registry.record_process_identity(agent_name="default", sid="main")
        process_registry.record_process_identity(agent_name="default", sid="spawned-1")
        marker = _marker(_isolated_processes_dir, os.getpid())
        [entry_a, entry_b] = marker["sessions"]
        assert {entry_a["sid"], entry_b["sid"]} == {"main", "spawned-1"}
    finally:
        process_registry.unregister_process(os.getpid())


# ── ended_at: pressed by the SAME #5694 callback, one entry only ───────────


@pytest.mark.asyncio
async def test_a_finished_sessions_task_marks_only_its_own_entry_ended(
    tmp_path: Path, _isolated_processes_dir: Path,
) -> None:
    """Tier 2: #5714 accept ④ — 2 co-hosted sessions, one's background
    task ends; ONLY that one's marker entry gains ``ended_at`` — the
    sibling entry is untouched. Driven through the real
    #5694/#5714-wired callback (``AgentRegistry._ensure_session_run`` /
    ``_on_session_run_task_done``), not a direct call to
    ``record_session_ended`` — this is the end-to-end wiring witness."""
    import os

    process_registry.register_process("chat")
    try:
        reg = _registry(tmp_path)
        default_session = reg.get_or_load("default")
        reg.create("coder-brown")
        brown_session = reg.get_or_load("coder-brown")

        task = reg._ensure_session_run("default", "main", default_session)
        reg._ensure_session_run("coder-brown", "main", brown_session)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Let the done-callback's own best-effort write actually land —
        # unbounded condition-wait (CLAUDE.md's own Ceiling rule), not a
        # fixed sleep.
        while True:
            marker = _marker(_isolated_processes_dir, os.getpid())
            default_entry = next(e for e in marker["sessions"] if e["agent_name"] == "default")
            if default_entry["ended_at"] is not None:
                break
            await asyncio.sleep(0)

        brown_entry = next(e for e in marker["sessions"] if e["agent_name"] == "coder-brown")
        assert brown_entry["ended_at"] is None, (
            "#5714 REGRESSION: the sibling session's own entry must stay "
            "untouched — only the ENDED session's entry gets ended_at"
        )

        brown_task = reg._tasks[("coder-brown", "main")]
        brown_task.cancel()
        try:
            await brown_task
        except asyncio.CancelledError:
            pass
    finally:
        process_registry.unregister_process(os.getpid())


def test_record_session_ended_has_exactly_one_production_caller() -> None:
    """Tier 2: #5714 accept ⑤ — the ruling's own explicit instruction:
    the push site for ``ended_at`` is the SAME #5694 callback, never a
    second lifecycle mechanism. Mirrors #5709's own structural
    git-grep witness for ``record_loop_beat``."""
    result = subprocess.run(
        ["git", "grep", "-n", "record_session_ended(", "--", "src/"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    hits = [
        line for line in result.stdout.splitlines()
        if ":def record_session_ended(" not in line and "``" not in line
    ]
    assert hits, "sanity: record_session_ended must have at least one real caller"
    # The call itself lives inside _on_session_run_task_done's own body —
    # that line does not repeat the method's own name, so the check is
    # "the only production call site is registry.py", not a name match.
    call_sites = [h for h in hits if "process_registry.py" not in h]
    try:
        [call_site] = call_sites
    except ValueError:
        raise AssertionError(
            f"#5714 REGRESSION: record_session_ended() must have exactly "
            f"one production call site, in registry.py's own #5694 "
            f"callback — got {call_sites!r}"
        ) from None
    assert "registry.py" in call_site


def test_a_session_that_never_recorded_an_identity_has_nothing_marked_ended(
    _isolated_processes_dir: Path,
) -> None:
    """Tier 2: deny side — record_session_ended for an (agent_name, sid)
    that was never recorded via record_process_identity must not
    fabricate an entry."""
    import os

    process_registry.register_process("chat")
    try:
        process_registry.record_session_ended(agent_name="never-recorded", sid="main")
        marker = _marker(_isolated_processes_dir, os.getpid())
        assert marker["sessions"] == [], (
            "#5714 REGRESSION: record_session_ended must never fabricate "
            "an entry for an identity it never recorded"
        )
    finally:
        process_registry.unregister_process(os.getpid())
