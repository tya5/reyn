"""Tier 2: the old chat_event-spelled identifier vocabulary is gone (#3794 P3).

Mechanical rename companion to P2 (#3838, prose, merged). Identifier sites
across 20 distinct forms (``_chat_events``, ``on_chat_event``,
``chat_events``, ``renderer_chat_events`` -> ``forwarded_frame_kinds``,
``subscribe_``/``unsubscribe_chat_events``, ``chat_event_types``,
``_forward_chat_event``, ``_is_self_chat_events_target``,
``_on_chat_event_for_state_change``, plus 8 test-function-name forms)
renamed to the ``audit_event``/``audit_events`` equivalent, per the design in
issue #3794 (architect comment). ``src/`` and ``tests/`` land in one PR —
splitting them breaks imports.

This scale is past "read and verify by hand", so correctness here is argued
by INVARIANT, not by review of every diff hunk (architect's own framing).
This module carries two of those invariants — completeness (zero old-spelled
identifiers remain) and same-object identity (the renamed public
``emit_audit_event`` seam and private ``_audit_events`` field still write the
SAME ``EventLog``) — checked against the REAL production module / a REAL
``Session``, nothing faked. The other two required invariants are pinned
elsewhere rather than here, because baking either into an assertion here
would itself be a Tier-4 size/shape pin (testing.md): total pytest collection
count unchanged (`pytest --collect-only -q`, before/after this rename) and
``AUDIT_EVENT_KINDS`` unchanged (the existing, already-CI-gated
``tests/core/test_audit_event_kind_vocabulary_3410.py`` staying green IS that
witness — this PR does not touch the vocabulary declaration itself, only a
comment-string identifier reference in ``event_schema.py``).
"""
from __future__ import annotations

import re
from pathlib import Path

from tests._support.agent_session import make_session
from tests._support.events import collect_events
from tests._support.paths import REPO_ROOT

EXCLUDED_DIR_NAMES = {".venv", "site", ".git", "__pycache__"}
SELF_PATH = Path(__file__).resolve()

# Underscore-joined identifier forms only — hyphen/space prose is P2's
# separate scope (#3838), already gated by its own completeness test.
_IDENTIFIER_PATTERN = re.compile(r"(?i)chat_events?")


def _identifier_target_files() -> list[Path]:
    files: list[Path] = []
    for sub in ("src", "tests", "docs"):
        root = REPO_ROOT / sub
        for path in root.rglob("*"):
            if not path.is_file() or path == SELF_PATH:
                continue
            if EXCLUDED_DIR_NAMES & set(path.relative_to(REPO_ROOT).parts):
                continue
            files.append(path)
    return files


def _identifier_hits(files: list[Path]) -> list[str]:
    hits = []
    for path in files:
        if _IDENTIFIER_PATTERN.search(path.name):
            hits.append(f"{path.relative_to(REPO_ROOT)} (filename)")
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _IDENTIFIER_PATTERN.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    return hits


def test_positive_control_the_scanner_actually_detects_the_old_spelling() -> None:
    """Tier 2: the pattern DOES fire on a known old-spelled identifier (guards the gate itself)."""
    known_positive = ["self._" + "chat_events" + ".emit(event_type, **data)"]
    hits = [line for line in known_positive if _IDENTIFIER_PATTERN.search(line)]
    assert hits, "the identifier pattern failed to match a known old-spelled site — the gate below would silently report a false zero"


def test_no_old_spelled_identifiers_remain_repo_wide() -> None:
    """Tier 2: zero old chat_event-spelled identifier occurrences remain (#3794 P3)."""
    hits = _identifier_hits(_identifier_target_files())
    assert not hits, "found remaining old-spelled identifiers (should read audit_event(s) now):\n" + "\n".join(hits)


def test_public_emit_seam_and_private_field_write_the_same_event_log(tmp_path) -> None:
    """Tier 2: renamed public ``emit_audit_event`` and private ``_audit_events`` are the SAME EventLog."""
    session = make_session(
        agent_name="p3-witness",
        snapshot_path=tmp_path / "snapshot.json",
    )
    collected = collect_events(session)
    before = len(collected)
    session.emit_audit_event("p3_witness_kind", marker="p3-rename-witness")
    after = collected
    assert len(after) == before + 1
    assert after[-1].type == "p3_witness_kind"
    assert after[-1].data.get("marker") == "p3-rename-witness"
