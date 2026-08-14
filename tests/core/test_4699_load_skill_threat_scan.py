"""Tier 2: #4699 — `load_skill` threat-scans the skill BODY, not just
`skill_install`'s frontmatter `description`.

`skill_install`'s own scan (`op_runtime/skill_install.py:494-530`) only
covers the description, and only runs when a skill is registered THROUGH
that op. A `.reyn/config/skills.yaml` entry written by hand never passes
through it. `load_skill` is the chokepoint every skill body crosses
regardless of how the entry was registered, so this is where the OS gate
actually lives (mirrors the existing `scan_tool_result`/`fence_tool_result`
tool-result chokepoint, not a new mechanism).

No mocks: real Workspace / PermissionResolver / EventLog / ThreatScanConfig
/ ThreatMatch throughout. Only `scan_for_threats` (the pattern-matching call
itself) is monkeypatched, at `load_skill`'s own import site — the same
technique `test_skill_install_pr_c.py`'s threat-scan test uses for the
sibling `skill_install` module, so a deterministic marker string drives the
block decision rather than depending on the real pattern catalog.
`first_blocking_match` is the REAL function (`content_guard.py`) — it is a
5-line pure function over `severity_blocks`'s rank comparison, not `==`, so
faking it would test the fake's own semantics instead of reyn's threshold
logic (a real bug in reyn's severity-ranking would not fail an `==`-based
fake).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.config.chat import ThreatScanConfig
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.load_skill import handle as load_skill_handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import LoadSkillIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.security.threat_patterns import ThreatMatch
from tests._support.events import collect_events


def _run(coro):
    return asyncio.run(coro)


def _make_ctx(project_root: Path, *, threat_scan=None) -> tuple[OpContext, EventLog]:
    events = EventLog()
    ws = Workspace(events=events, base_dir=project_root)
    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False,
    )
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        actor="test_load_skill_threat_scan",
        threat_scan=threat_scan,
    )
    return ctx, events


def _write_skill(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ── the central witness: a skill body reaching load_skill through ANY
# registration path — including one that never went through skill_install's
# own description-only scan — still gets its BODY scanned at load time ─────


def test_load_skill_blocks_a_body_matching_a_threat_pattern(tmp_path, monkeypatch):
    """Tier 2: #4699's own reason for existing — `load_skill` scans the BODY
    regardless of how the entry reached `skills.yaml` (this op does its own
    read + scan; it never consults `skill_install`'s own description-only
    scan, so a hand-written config entry is covered too). RED without the
    fix: status='ok' and the threatening body reaches `content`."""
    project_root = tmp_path / "project"
    skill_path = project_root / "skills" / "evil" / "SKILL.md"
    _write_skill(
        skill_path,
        "---\nname: evil\ndescription: a perfectly innocent description\n---\n"
        "EVIL_BODY_THREAT_MARKER\n",
    )
    threat_config = ThreatScanConfig()  # real type, every field its own default
    ctx, events = _make_ctx(project_root, threat_scan=threat_config)
    collected = collect_events(events)

    def _fake_scan(content, config, *, scope="context"):
        if "EVIL_BODY_THREAT_MARKER" in content:
            return [ThreatMatch(pattern_id="test-threat", scope="strict", severity="block")]
        return []

    monkeypatch.setattr("reyn.core.op_runtime.load_skill.scan_for_threats", _fake_scan)

    result = _run(load_skill_handle(
        LoadSkillIROp(kind="load_skill", path=str(skill_path)), ctx,
    ))

    assert result["status"] == "blocked", result
    assert result["content"] == "", "a blocked body must never reach content"
    assert "EVIL_BODY_THREAT_MARKER" not in result["content"]
    blocked = [e for e in collected if e.type == "skill_body_threat_blocked"]
    assert blocked, "no skill_body_threat_blocked event was emitted"
    assert blocked[-1].data["pattern_id"] == "test-threat"


# ── accept-side: a clean body loads normally ────────────────────────────────


def test_load_skill_does_not_block_a_clean_body(tmp_path, monkeypatch):
    """Tier 2: (accept-side) a body with no threat match loads status='ok'
    with its real content — the scan must not false-positive on ordinary
    skill bodies."""
    project_root = tmp_path / "project"
    skill_path = project_root / "skills" / "clean" / "SKILL.md"
    _write_skill(skill_path, "---\nname: clean\n---\nJust a normal skill body.\n")

    threat_config = ThreatScanConfig()
    ctx, events = _make_ctx(project_root, threat_scan=threat_config)

    def _fake_scan(content, config, *, scope="context"):
        return []  # nothing ever matches

    monkeypatch.setattr("reyn.core.op_runtime.load_skill.scan_for_threats", _fake_scan)

    result = _run(load_skill_handle(
        LoadSkillIROp(kind="load_skill", path=str(skill_path)), ctx,
    ))

    assert result["status"] == "ok", result
    assert "Just a normal skill body." in result["content"]


# ── non-blocking match: recorded but not blocked ────────────────────────────


def test_load_skill_emits_match_event_without_blocking_for_a_low_severity_match(tmp_path, monkeypatch):
    """Tier 2: (accept-side) a match below the block-severity threshold is
    recorded via `skill_body_threat_match` but does not prevent the load —
    mirrors `skill_install`'s own match-vs-block distinction. Drives the
    REAL `first_blocking_match`/`severity_blocks` rank comparison (`warn` <
    `block`), not a stand-in — a "warn" match must not block against the
    default `block_severity="block"` threshold."""
    project_root = tmp_path / "project"
    skill_path = project_root / "skills" / "warn" / "SKILL.md"
    _write_skill(skill_path, "---\nname: warn\n---\nMILD_MARKER body text.\n")

    threat_config = ThreatScanConfig()  # block_severity="block" (default)
    ctx, events = _make_ctx(project_root, threat_scan=threat_config)
    collected = collect_events(events)

    def _fake_scan(content, config, *, scope="context"):
        if "MILD_MARKER" in content:
            return [ThreatMatch(pattern_id="mild-threat", scope="strict", severity="warn")]
        return []

    monkeypatch.setattr("reyn.core.op_runtime.load_skill.scan_for_threats", _fake_scan)

    result = _run(load_skill_handle(
        LoadSkillIROp(kind="load_skill", path=str(skill_path)), ctx,
    ))

    assert result["status"] == "ok", result
    assert "MILD_MARKER" in result["content"]
    matched = [e for e in collected if e.type == "skill_body_threat_match"]
    assert matched, "no skill_body_threat_match event was emitted"
    blocked = [e for e in collected if e.type == "skill_body_threat_blocked"]
    assert not blocked


# ── disabled scan: byte-identical to before #4699 ───────────────────────────


def test_load_skill_skips_scan_when_threat_scan_disabled(tmp_path, monkeypatch):
    """Tier 2: (accept-side) `ctx.threat_scan` absent (None, the pre-#4699
    default) — no scan runs, no event, content unaffected. Guards against
    a regression that makes threat-scan a hard dependency for every
    load_skill call, including test/phase-fallback OpContexts that never
    set `threat_scan`."""
    project_root = tmp_path / "project"
    skill_path = project_root / "skills" / "unscanned" / "SKILL.md"
    _write_skill(skill_path, "---\nname: unscanned\n---\nAnything at all.\n")

    ctx, events = _make_ctx(project_root, threat_scan=None)
    collected = collect_events(events)

    def _fail_if_called(*_a, **_k):
        raise AssertionError("scan_for_threats must not be called when threat_scan is None")

    monkeypatch.setattr("reyn.core.op_runtime.load_skill.scan_for_threats", _fail_if_called)

    result = _run(load_skill_handle(
        LoadSkillIROp(kind="load_skill", path=str(skill_path)), ctx,
    ))

    assert result["status"] == "ok", result
    assert "Anything at all." in result["content"]
    assert not [e for e in collected if e.type in ("skill_body_threat_match", "skill_body_threat_blocked")]
