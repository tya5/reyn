"""Tier 2: #4701 — `file.read` threat-scans a skill's REFERENCE files (any
file under a registered skill's own directory) with the SAME strict+block
treatment #4699 already gives the SKILL.md body itself.

Owner ruling (#4701 comment thread): a skill's reference files are the SAME
content class as its SKILL.md body — both are instructions the model reads
and follows — so `load_skill.py`'s strict+block scan alone left a real gap:
`references/*.md` is opened through the ordinary `file.read` op (the
model's own choice, per the body's text), which carried none of that
enforcement.

No mocks: real Workspace / PermissionResolver / EventLog / SkillEntry /
ThreatScanConfig / ThreatMatch throughout. Only `scan_for_threats` is
monkeypatched at `file.py`'s own import site (same technique
`test_4699_load_skill_threat_scan.py` uses for the sibling `load_skill`
module) — `first_blocking_match` is the REAL function.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from reyn.config.chat import ThreatScanConfig
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle as file_handle
from reyn.data.skills.registry import SkillEntry
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.security.threat_patterns import ThreatMatch
from tests._support.events import collect_events


def _run(coro):
    return asyncio.run(coro)


def _make_ctx(
    project_root: Path, *, threat_scan=None, available_skills=None,
) -> tuple[OpContext, EventLog]:
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
        actor="test_skill_reference_threat_scan",
        threat_scan=threat_scan,
        available_skills=available_skills,
    )
    return ctx, events


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ── the central witness: a reference file under a REGISTERED skill's own
# directory is blocked the same way the body is ──────────────────────────


def test_reference_under_a_skill_directory_is_blocked(tmp_path, monkeypatch):
    """Tier 2: #4701's own reason for existing — a file under a registered
    skill's directory (e.g. `references/notes.md`), opened through the
    ORDINARY file.read op, is threat-scanned at strict+block — the SAME
    treatment the SKILL.md body gets from load_skill.py. RED without the
    fix: status='ok' and the threatening reference content reaches
    `content`."""
    project_root = tmp_path / "project"
    skill_md = project_root / "skills" / "my-skill" / "SKILL.md"
    _write(skill_md, "---\nname: my-skill\n---\nSee references/notes.md.\n")
    ref_path = project_root / "skills" / "my-skill" / "references" / "notes.md"
    _write(ref_path, "EVIL_REFERENCE_THREAT_MARKER\n")

    entry = SkillEntry(name="my-skill", description="d", path=str(skill_md))
    threat_config = ThreatScanConfig()
    ctx, events = _make_ctx(project_root, threat_scan=threat_config, available_skills=[entry])
    collected = collect_events(events)

    def _fake_scan(content, config, *, scope="context"):
        if "EVIL_REFERENCE_THREAT_MARKER" in content:
            return [ThreatMatch(pattern_id="test-threat", scope="strict", severity="block")]
        return []

    monkeypatch.setattr("reyn.core.op_runtime.file.scan_for_threats", _fake_scan)

    result = _run(file_handle(
        FileIROp(kind="file", op="read", path=str(ref_path)), ctx,
    ))

    assert result["status"] == "blocked", result
    assert result["content"] == ""
    assert "EVIL_REFERENCE_THREAT_MARKER" not in result["content"]
    blocked = [e for e in collected if e.type == "skill_body_threat_blocked"]
    assert blocked, "no skill_body_threat_blocked event was emitted"
    assert blocked[-1].data["pattern_id"] == "test-threat"


# ── fence: a CLEAN (non-blocked) skill reference is still tagged external ──


def test_clean_skill_reference_read_is_tagged_external_source(tmp_path, monkeypatch):
    """Tier 2: #4701 (lead-coder review, fence condition) — a skill reference
    read that does NOT trip the threat scan is still tagged
    `_external_source=True` on the op's own result dict — the per-call
    dynamic override `router_loop.py`'s `_execute_all` reads (an op-level
    True is never reset by the static `returns_external_content` check).
    This is what makes `router_loop.py`'s fence wrap this content at the
    tool-result chokepoint, mirroring the SAME treatment `load_skill`'s
    static `returns_external_content=True` gives the SKILL.md body."""
    project_root = tmp_path / "project"
    skill_md = project_root / "skills" / "my-skill" / "SKILL.md"
    _write(skill_md, "---\nname: my-skill\n---\nbody\n")
    ref_path = project_root / "skills" / "my-skill" / "references" / "notes.md"
    _write(ref_path, "A clean, harmless reference.\n")

    entry = SkillEntry(name="my-skill", description="d", path=str(skill_md))
    threat_config = ThreatScanConfig()
    ctx, _events = _make_ctx(project_root, threat_scan=threat_config, available_skills=[entry])

    def _fake_scan(content, config, *, scope="context"):
        return []  # nothing matches — the clean-content path

    monkeypatch.setattr("reyn.core.op_runtime.file.scan_for_threats", _fake_scan)

    result = _run(file_handle(
        FileIROp(kind="file", op="read", path=str(ref_path)), ctx,
    ))

    assert result["status"] == "ok", result
    assert result.get("_external_source") is True


def test_ordinary_file_read_is_not_tagged_external_source(tmp_path):
    """Tier 2: (accept-side) an ordinary read — no registered skills at all
    — is NEVER tagged `_external_source`, matching the pre-#4701 default
    (`returns_external_content=False`) for every read_file call that isn't
    a skill reference."""
    project_root = tmp_path / "project"
    plain = project_root / "src" / "app.py"
    _write(plain, "print('hello')\n")

    ctx, _events = _make_ctx(project_root)  # no threat_scan, no available_skills

    result = _run(file_handle(
        FileIROp(kind="file", op="read", path=str(plain)), ctx,
    ))

    assert result["status"] == "ok", result
    assert "_external_source" not in result


def test_ordinary_file_outside_any_skill_directory_is_unaffected(tmp_path, monkeypatch):
    """Tier 2: (accept-side) a plain project file — NOT under any registered
    skill's directory — is never scanned at all: read_file's other callers
    are completely unaffected (the #4701 ruling's explicit boundary: making
    every read strict+block would spread false-positives across all file
    reading)."""
    project_root = tmp_path / "project"
    plain = project_root / "src" / "app.py"
    _write(plain, "EVIL_REFERENCE_THREAT_MARKER\n")  # same marker, different location

    threat_config = ThreatScanConfig()
    unrelated_entry = SkillEntry(
        name="unrelated", description="d",
        path=str(project_root / "skills" / "unrelated" / "SKILL.md"),
    )
    ctx, events = _make_ctx(project_root, threat_scan=threat_config, available_skills=[unrelated_entry])
    collected = collect_events(events)

    def _fail_if_called(*_a, **_k):
        raise AssertionError("scan_for_threats must not run outside a skill directory")

    monkeypatch.setattr("reyn.core.op_runtime.file.scan_for_threats", _fail_if_called)

    result = _run(file_handle(
        FileIROp(kind="file", op="read", path=str(plain)), ctx,
    ))

    assert result["status"] == "ok", result
    assert "EVIL_REFERENCE_THREAT_MARKER" in result["content"]
    assert not [e for e in collected if e.type in ("skill_body_threat_match", "skill_body_threat_blocked")]


# ── lead-coder follow-up: a directory-form SkillEntry.path must not widen
# containment to the parent of the SKILLS directory ─────────────────────────


def test_a_directory_form_entry_path_does_not_scan_sibling_skills(tmp_path, monkeypatch):
    """Tier 2: #4701 follow-up (lead-coder review) — `SkillEntry.path` may
    name SKILL.md directly OR its containing directory (`registry.py`'s own
    docstring: "as declared"; the registry does not normalize). Taking
    `.parent` unconditionally, without first normalizing via
    `_resolve_skill_md`, would treat a directory-form entry
    (`path: "skills/my-skill"`) as if its OWN directory were `skills/` —
    pulling every SIBLING skill's files into strict+block scope. RED
    without the fix: a sibling skill's ordinary file gets scanned even
    though it belongs to a DIFFERENT skill than the registered entry."""
    project_root = tmp_path / "project"
    my_skill_dir = project_root / "skills" / "my-skill"
    _write(my_skill_dir / "SKILL.md", "---\nname: my-skill\n---\nbody\n")
    sibling_file = project_root / "skills" / "sibling-skill" / "notes.md"
    _write(sibling_file, "EVIL_REFERENCE_THREAT_MARKER\n")

    # Directory-form path (no SKILL.md suffix) — the shape the registry
    # accepts as-is per its own docstring.
    entry = SkillEntry(name="my-skill", description="d", path=str(my_skill_dir))
    threat_config = ThreatScanConfig()
    ctx, events = _make_ctx(project_root, threat_scan=threat_config, available_skills=[entry])
    collected = collect_events(events)

    def _fail_if_called(*_a, **_k):
        raise AssertionError("scan_for_threats must not run on a SIBLING skill's file")

    monkeypatch.setattr("reyn.core.op_runtime.file.scan_for_threats", _fail_if_called)

    result = _run(file_handle(
        FileIROp(kind="file", op="read", path=str(sibling_file)), ctx,
    ))

    assert result["status"] == "ok", result
    assert "EVIL_REFERENCE_THREAT_MARKER" in result["content"]
    assert not [e for e in collected if e.type in ("skill_body_threat_match", "skill_body_threat_blocked")]


def test_the_skill_own_directory_covers_nested_reference_subdirectories(tmp_path, monkeypatch):
    """Tier 2: (accept-side) containment is not limited to a literal
    `references/` name — ANY path under the skill's own directory tree
    counts as the same content class (a deeper-nested file included)."""
    project_root = tmp_path / "project"
    skill_md = project_root / "skills" / "my-skill" / "SKILL.md"
    _write(skill_md, "---\nname: my-skill\n---\nbody\n")
    nested = project_root / "skills" / "my-skill" / "docs" / "deep" / "notes.md"
    _write(nested, "EVIL_REFERENCE_THREAT_MARKER\n")

    entry = SkillEntry(name="my-skill", description="d", path=str(skill_md))
    threat_config = ThreatScanConfig()
    ctx, events = _make_ctx(project_root, threat_scan=threat_config, available_skills=[entry])

    def _fake_scan(content, config, *, scope="context"):
        if "EVIL_REFERENCE_THREAT_MARKER" in content:
            return [ThreatMatch(pattern_id="test-threat", scope="strict", severity="block")]
        return []

    monkeypatch.setattr("reyn.core.op_runtime.file.scan_for_threats", _fake_scan)

    result = _run(file_handle(
        FileIROp(kind="file", op="read", path=str(nested)), ctx,
    ))

    assert result["status"] == "blocked", result


def test_no_threat_scan_config_leaves_reference_read_unaffected(tmp_path, monkeypatch):
    """Tier 2: (accept-side) `ctx.threat_scan` absent (None) — no scan runs
    even for a path under a registered skill's directory, matching the
    #4699 precedent's own "no scan without config" contract."""
    project_root = tmp_path / "project"
    skill_md = project_root / "skills" / "my-skill" / "SKILL.md"
    _write(skill_md, "---\nname: my-skill\n---\nbody\n")
    ref_path = project_root / "skills" / "my-skill" / "references" / "notes.md"
    _write(ref_path, "Anything at all.\n")

    entry = SkillEntry(name="my-skill", description="d", path=str(skill_md))
    ctx, events = _make_ctx(project_root, threat_scan=None, available_skills=[entry])

    def _fail_if_called(*_a, **_k):
        raise AssertionError("scan_for_threats must not run when threat_scan is None")

    monkeypatch.setattr("reyn.core.op_runtime.file.scan_for_threats", _fail_if_called)

    result = _run(file_handle(
        FileIROp(kind="file", op="read", path=str(ref_path)), ctx,
    ))

    assert result["status"] == "ok", result
    assert "Anything at all." in result["content"]


# ── condition ②: an undeterminable resolution errs toward scanning ─────────


def test_a_broken_registered_skill_path_errs_toward_scanning(tmp_path, monkeypatch):
    """Tier 2: #4701 (lead-coder review, condition ②) — a registered skill
    ENTRY whose own declared path cannot be resolved (a genuine symlink
    LOOP — the exact evasion shape ② names: "a single symlink must not be
    able to evade the check") must NOT silently let an unrelated read skip
    the scan. The read here has NOTHING to do with the broken entry, but
    because that entry's resolution is undeterminable, the whole
    registry-derived answer errs toward "treat as skill content" rather
    than quietly excluding the broken entry from consideration.

    ``RuntimeError`` (not ``OSError``) is the load-bearing detail: a
    symlink loop raises ``RuntimeError("Symlink loop from ...")`` from
    ``Path.resolve()`` on the installed Python, confirmed live — a fix
    that only caught ``OSError`` would leave this uncaught (crashing the
    whole read) rather than degrading safely."""
    project_root = tmp_path / "project"
    # A genuine symlink loop for the REGISTERED skill's own path.
    loop_a = project_root / "skills" / "broken" / "a"
    loop_b = project_root / "skills" / "broken" / "b"
    loop_a.parent.mkdir(parents=True)
    os.symlink(loop_b, loop_a)
    os.symlink(loop_a, loop_b)

    unrelated_read = project_root / "src" / "app.py"
    _write(unrelated_read, "EVIL_REFERENCE_THREAT_MARKER\n")

    broken_entry = SkillEntry(name="broken", description="d", path=str(loop_a))
    threat_config = ThreatScanConfig()
    ctx, events = _make_ctx(project_root, threat_scan=threat_config, available_skills=[broken_entry])

    def _fake_scan(content, config, *, scope="context"):
        if "EVIL_REFERENCE_THREAT_MARKER" in content:
            return [ThreatMatch(pattern_id="test-threat", scope="strict", severity="block")]
        return []

    monkeypatch.setattr("reyn.core.op_runtime.file.scan_for_threats", _fake_scan)

    result = _run(file_handle(
        FileIROp(kind="file", op="read", path=str(unrelated_read)), ctx,
    ))

    assert result["status"] == "blocked", result
