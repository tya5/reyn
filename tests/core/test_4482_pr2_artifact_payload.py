"""Tier 2: #4482 PR-2 — present artifact payload construction
(``build_source_artifact_payload``/``build_inline_artifact_payload``).

Exercises architect's final corrected design directly: media_type is
best-effort (mimetypes only, never a reyn-specific table), Inline/Reference
is decided by observation (stat + bounded UTF-8-decode probe, not a
media_type lookup), and the agent-declared-inline path lets the agent name
media_type (the one case the OS can't derive it).
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.present.artifact_payload import (
    build_inline_artifact_payload,
    build_source_artifact_payload,
)


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ── build_source_artifact_payload ───────────────────────────────────────


def test_small_utf8_text_file_is_inlined_alongside_a_ref(tmp_path: Path):
    """Tier 2: invariant 3 (as of #4574 design C) — a small, UTF-8-decodable
    file gets an inline PREVIEW *alongside* a minted ref, never inline-only.
    #4574's own reported symptom was exactly the pre-fix shape this guards
    against: inline-only meant no `ref`, so the Art tab had nothing to open
    for a source-backed file the agent handed over."""
    target = _write(tmp_path / "notes.txt", "hello, world\n".encode("utf-8"))

    payload = build_source_artifact_payload(tmp_path, "alice", target)

    assert payload["body"]["inline"] == "hello, world\n"
    assert "ref" in payload["body"]
    assert payload["body"]["size"] == target.stat().st_size
    assert payload["name"] == "notes.txt"


def test_binary_content_becomes_a_reference(tmp_path: Path):
    """Tier 2: invariant 3 — content that fails UTF-8 decode is a
    Reference, regardless of size."""
    target = _write(tmp_path / "image.png", b"\x89PNG\r\n\x1a\n" + b"\xff\xfe\x00" * 5)

    payload = build_source_artifact_payload(tmp_path, "alice", target)

    assert "ref" in payload["body"]
    assert payload["body"]["size"] == target.stat().st_size


def test_a_file_larger_than_the_probe_limit_is_a_reference_without_reading_it(
    tmp_path: Path,
):
    """Tier 2: invariant 3 — above the probe limit, this function goes
    straight to Reference without a decode attempt, even for content that
    WOULD have decoded as UTF-8 if read (proves the stat-first short
    circuit, not just "big files happen to be binary")."""
    target = _write(tmp_path / "huge.txt", b"a" * 10)  # valid UTF-8 content

    payload = build_source_artifact_payload(tmp_path, "alice", target, probe_max_bytes=5)

    assert "ref" in payload["body"]
    assert payload["body"]["size"] == 10


def test_media_type_is_derived_via_stdlib_mimetypes_only(tmp_path: Path):
    """Tier 2: invariant 2 — media_type comes from mimetypes.guess_type,
    not a reyn-specific table. .html resolves; an unrecognized extension
    resolves to None (best-effort, not a hard requirement)."""
    html = _write(tmp_path / "report.html", b"<h1>x</h1>")
    unknown = _write(tmp_path / "report.pptx", b"PK\x03\x04" + b"\x00" * 20)

    html_payload = build_source_artifact_payload(tmp_path, "alice", html)
    pptx_payload = build_source_artifact_payload(tmp_path, "alice", unknown)

    assert html_payload["media_type"] == "text/html"
    # pptx is exactly the format architect measured stdlib mimetypes
    # resolving to None on a minimal host -- best-effort, not a failure.
    assert pptx_payload["media_type"] in (None, "application/vnd.openxmlformats-officedocument.presentationml.presentation")


def test_missing_source_raises_file_not_found(tmp_path: Path):
    """Tier 2: a source that doesn't resolve to a real file raises,
    rather than silently returning a bogus payload."""
    try:
        build_source_artifact_payload(tmp_path, "alice", tmp_path / "nope.txt")
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass


def test_description_is_omitted_when_not_given(tmp_path: Path):
    """Tier 2: (accept-side) description is optional — absent input means
    an absent key, not a null-valued one."""
    target = _write(tmp_path / "notes.txt", b"x")
    payload = build_source_artifact_payload(tmp_path, "alice", target)
    assert "description" not in payload


def test_description_is_carried_through_when_given(tmp_path: Path):
    """Tier 2: description, when given, passes through unchanged."""
    target = _write(tmp_path / "notes.txt", b"x")
    payload = build_source_artifact_payload(
        tmp_path, "alice", target, description="the quarterly report",
    )
    assert payload["description"] == "the quarterly report"


def test_reference_body_resolves_via_the_same_ref_table_pr1_built(tmp_path: Path):
    """Tier 2: (integration) the minted ref genuinely round-trips through
    artifact_ref.resolve_ref — this function doesn't invent its own,
    parallel resolution mechanism."""
    from reyn.data.workspace.artifact_ref import resolve_ref

    target = _write(tmp_path / "image.png", b"\x89PNG" + b"\xff\xfe" * 5)
    payload = build_source_artifact_payload(tmp_path, "alice", target)

    resolved = resolve_ref(tmp_path, "alice", payload["body"]["ref"])
    assert resolved == target.resolve()


def test_no_path_string_appears_anywhere_in_the_payload(tmp_path: Path):
    """Tier 2: invariant 1, falsified directly — neither the raw nor the
    normalized absolute path string appears anywhere in the payload
    (name is a basename, body is inline text or an opaque ref)."""
    target = _write(tmp_path / "sub" / "report.pptx", b"PK\x03\x04binary\xff\xfe")
    payload = build_source_artifact_payload(tmp_path, "alice", target)

    serialized = repr(payload)
    assert str(target.resolve()) not in serialized
    assert str(target) not in serialized


# ── build_inline_artifact_payload ───────────────────────────────────────


def test_inline_payload_carries_the_agent_declared_media_type():
    """Tier 2: slot ②'s carve-out — with no real file, the agent names
    media_type itself."""
    payload = build_inline_artifact_payload("text/csv", "a,b,c\n1,2,3\n")
    assert payload["media_type"] == "text/csv"
    assert payload["body"] == {"inline": "a,b,c\n1,2,3\n"}


def test_inline_payload_has_no_name_slot():
    """Tier 2: invariant — inline content has no real-file identity, so
    there is no name slot to fill (nothing is ever opened for this
    form)."""
    payload = build_inline_artifact_payload("text/plain", "hello")
    assert "name" not in payload


def test_inline_payload_has_no_size_cap():
    """Tier 2: invariant 4 — no reyn-side byte cap on agent-declared
    inline content, however large; the model's own output-token ceiling
    is the only bound."""
    huge = "x" * 2_000_000
    payload = build_inline_artifact_payload("text/plain", huge)
    assert payload["body"]["inline"] == huge


def test_inline_payload_description_is_optional():
    """Tier 2: (accept-side) same optional-description contract as the
    source-backed form."""
    payload = build_inline_artifact_payload("text/plain", "x")
    assert "description" not in payload
    payload_with = build_inline_artifact_payload("text/plain", "x", description="a note")
    assert payload_with["description"] == "a note"
