"""Tier 2: #4699 — `load_skill` threat-scans the skill BODY, not just
`skill_install`'s frontmatter `description`.

`skill_install`'s own scan (`op_runtime/skill_install.py:494-530`) only
covers the description, and only runs when a skill is registered THROUGH
that op. A `.reyn/config/skills.yaml` entry written by hand never passes
through it. `load_skill` is the chokepoint every skill body crosses
regardless of how the entry was registered, so this is where the OS gate
actually lives (mirrors the existing `scan_tool_result`/`fence_tool_result`
tool-result chokepoint, not a new mechanism).

No mocks: real Workspace / PermissionResolver / EventLog / SkillEntry
throughout. `scan_for_threats`/`first_blocking_match` are monkeypatched at
the `load_skill` module's own import site — the same technique
`test_skill_install_pr_c.py`'s threat-scan test already uses for the
sibling `skill_install` module, so a real `ThreatMatch`-shaped stub drives
the block decision rather than depending on the real pattern catalog.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.load_skill import handle as load_skill_handle
from reyn.data.skills.registry import SkillEntry
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import LoadSkillIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from tests._support.events import collect_events


def _run(coro):
    return asyncio.run(coro)


class _ThreatMatch:
    def __init__(self, pattern_id: str = "test-threat", severity: str = "block", scope: str = "strict"):
        self.pattern_id = pattern_id
        self.severity = severity
        self.scope = scope


class _FakeThreatScanConfig:
    def __init__(self, *, enabled: bool = True, block_severity: str = "block"):
        self.enabled = enabled
        self.block_severity = block_severity


def _make_ctx(project_root: Path, *, threat_scan=None, available_skills=None) -> tuple[OpContext, EventLog]:
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
        available_skills=available_skills,
    )
    return ctx, events


def _write_skill(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ── the central witness: a HAND-WRITTEN skills.yaml entry (never installed
# through skill_install) still gets its body scanned at load time ──────────


def test_load_skill_blocks_a_hand_registered_skill_with_a_threatening_body(tmp_path, monkeypatch):
    """Tier 2: #4699's own reason for existing — a skill registered by
    directly hand-writing `skills.yaml` (never through `skill_install`,
    so its description-only scan never ran) still gets its BODY scanned
    the moment `load_skill` reads it. RED without the fix: status='ok'
    and the threatening body reaches `content`."""
    project_root = tmp_path / "project"
    skill_path = project_root / "skills" / "evil" / "SKILL.md"
    _write_skill(
        skill_path,
        "---\nname: evil\ndescription: a perfectly innocent description\n---\n"
        "EVIL_BODY_THREAT_MARKER\n",
    )
    # Simulate a hand-written skills.yaml entry: a SkillEntry that exists in
    # the live registry snapshot WITHOUT ever having gone through
    # skill_install's own description scan.
    entry = SkillEntry(
        name="evil", description="a perfectly innocent description",
        path=str(skill_path),
    )
    threat_config = _FakeThreatScanConfig()
    ctx, events = _make_ctx(project_root, threat_scan=threat_config, available_skills=[entry])
    collected = collect_events(events)

    def _fake_scan(content, config, *, scope="context"):
        if "EVIL_BODY_THREAT_MARKER" in content:
            return [_ThreatMatch()]
        return []

    monkeypatch.setattr("reyn.core.op_runtime.load_skill.scan_for_threats", _fake_scan)
    monkeypatch.setattr(
        "reyn.core.op_runtime.load_skill.first_blocking_match",
        lambda matches, threshold="block": matches[0] if matches else None,
    )

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

    threat_config = _FakeThreatScanConfig()
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
    mirrors `skill_install`'s own match-vs-block distinction."""
    project_root = tmp_path / "project"
    skill_path = project_root / "skills" / "warn" / "SKILL.md"
    _write_skill(skill_path, "---\nname: warn\n---\nMILD_MARKER body text.\n")

    threat_config = _FakeThreatScanConfig(block_severity="block")
    ctx, events = _make_ctx(project_root, threat_scan=threat_config)
    collected = collect_events(events)

    def _fake_scan(content, config, *, scope="context"):
        if "MILD_MARKER" in content:
            return [_ThreatMatch(severity="warn")]
        return []

    monkeypatch.setattr("reyn.core.op_runtime.load_skill.scan_for_threats", _fake_scan)
    monkeypatch.setattr(
        "reyn.core.op_runtime.load_skill.first_blocking_match",
        lambda matches, threshold="block": next(
            (m for m in matches if m.severity == threshold), None,
        ),
    )

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
