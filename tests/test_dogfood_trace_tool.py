"""Tests for scripts/dogfood_trace.py — standalone batch observation tool.

Tier 2: OS invariant — verifies the tool's public CLI surface and output
structure without touching any reyn package internals.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "dogfood_trace.py"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _write_events(reyn_dir: Path, events: list[dict], subpath: str = "events/agents/default/chat/session.jsonl") -> Path:
    target = reyn_dir / subpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return target


def _write_ledger(reyn_dir: Path, entries: list[dict]) -> Path:
    ledger = reyn_dir / "state" / "budget_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return ledger


def _run(args: list[str]) -> tuple[str, int]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True, text=True
    )
    return result.stdout + result.stderr, result.returncode


MINIMAL_EVENTS = [
    {"type": "workflow_started", "timestamp": "2026-05-04T10:00:00+09:00",
     "data": {"run_id": "run_001", "skill": "my_skill", "entry_phase": "do_it"}},
    {"type": "phase_started", "timestamp": "2026-05-04T10:00:01+09:00",
     "data": {"phase": "do_it", "visit_count": 1}},
    {"type": "tool_called", "timestamp": "2026-05-04T10:00:02+09:00",
     "data": {"caller_kind": "skill_phase", "caller_id": "my_skill.do_it",
              "tool": "invoke_skill", "args": {"skill": "sub"}}},
    {"type": "tool_called", "timestamp": "2026-05-04T10:00:03+09:00",
     "data": {"caller_kind": "skill_phase", "caller_id": "my_skill.do_it",
              "tool": "describe_skill", "args": {"name": "sub"}}},
    {"type": "workflow_finished", "timestamp": "2026-05-04T10:00:05+09:00",
     "data": {"run_id": "run_001", "status": "finished"}},
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSummaryMode:
    """Tier 2: summary mode emits expected sections."""

    def test_summary_includes_skill_chain(self, tmp_path: Path) -> None:
        """Tier 2: summary output contains skill_started and tool_called data."""
        reyn = tmp_path / ".reyn"
        _write_events(reyn, MINIMAL_EVENTS)
        out, rc = _run(["--root", str(reyn), "--mode", "summary"])
        assert rc == 0
        assert "my_skill" in out
        assert "invoke_skill" in out or "describe_skill" in out

    def test_summary_no_events_message(self, tmp_path: Path) -> None:
        """Tier 2: empty .reyn/events dir prints 'no events found'."""
        reyn = tmp_path / ".reyn"
        reyn.mkdir()
        out, rc = _run(["--root", str(reyn), "--mode", "summary"])
        assert rc == 0
        assert "no events found" in out

    def test_summary_missing_root(self, tmp_path: Path) -> None:
        """Tier 2: non-existent root exits 0 with 'no events found'."""
        out, rc = _run(["--root", str(tmp_path / "nonexistent"), "--mode", "summary"])
        assert rc == 0
        assert "no events found" in out


class TestCostMode:
    """Tier 2: cost mode parses ledger correctly."""

    def test_cost_from_ledger(self, tmp_path: Path) -> None:
        """Tier 2: cost mode reads budget_ledger.jsonl and shows total USD."""
        reyn = tmp_path / ".reyn"
        _write_ledger(reyn, [
            {"ts": "2026-05-04T10:00:00+09:00", "model": "gemini-2.5-flash-lite", "tokens": 1000, "cost_usd": 0.0001},
            {"ts": "2026-05-04T10:00:01+09:00", "model": "gemini-2.5-flash-lite", "tokens": 500,  "cost_usd": 0.00005},
        ])
        out, rc = _run(["--root", str(reyn), "--mode", "cost"])
        assert rc == 0
        assert "0.00015" in out or "0.000150" in out

    def test_cost_no_ledger(self, tmp_path: Path) -> None:
        """Tier 2: cost mode with no ledger prints 'no cost ledger found'."""
        reyn = tmp_path / ".reyn"
        reyn.mkdir()
        out, rc = _run(["--root", str(reyn), "--mode", "cost"])
        assert rc == 0
        assert "no cost ledger found" in out


class TestFullMode:
    """Tier 2: full mode groups events by kind."""

    def test_full_filter(self, tmp_path: Path) -> None:
        """Tier 2: --mode full --filter tool_called shows only that event kind."""
        reyn = tmp_path / ".reyn"
        _write_events(reyn, MINIMAL_EVENTS)
        out, rc = _run(["--root", str(reyn), "--mode", "full", "--filter", "tool_called"])
        assert rc == 0
        assert "tool_called" in out
        # workflow_started events should NOT appear in the header section
        assert "workflow_started" not in out

    def test_full_corrupt_line_skipped(self, tmp_path: Path) -> None:
        """Tier 2: a corrupt JSONL line is silently skipped; valid lines parsed."""
        reyn = tmp_path / ".reyn"
        target = reyn / "events" / "agents" / "default" / "chat" / "s.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            '{"type":"workflow_started","timestamp":"2026-05-04T10:00:00+09:00","data":{"run_id":"r1","skill":"x","entry_phase":"p"}}\n'
            'NOT VALID JSON\n'
            '{"type":"workflow_finished","timestamp":"2026-05-04T10:00:01+09:00","data":{"run_id":"r1","status":"finished"}}\n',
            encoding="utf-8",
        )
        out, rc = _run(["--root", str(reyn), "--mode", "full"])
        assert rc == 0
        assert "workflow_started" in out
        assert "workflow_finished" in out


class TestChainMode:
    """Tier 2: chain mode shows workflow/phase/tool timeline."""

    def test_chain_shows_phases(self, tmp_path: Path) -> None:
        """Tier 2: chain mode prints phase_started events in order."""
        reyn = tmp_path / ".reyn"
        _write_events(reyn, MINIMAL_EVENTS)
        out, rc = _run(["--root", str(reyn), "--mode", "chain"])
        assert rc == 0
        assert "workflow_started" in out
        assert "do_it" in out
