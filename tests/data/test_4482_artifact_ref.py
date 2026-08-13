"""Tier 2: #4482 PR-1 — the ref -> path table (``mint_ref``/``resolve_ref``).

Real on-disk state under ``tmp_path`` throughout, no mocks. Exercises the
acceptance criteria lead-coder's brief listed verbatim: idempotent minting,
normalization-insensitive minting, resolve-after-mint, unresolvable-on-
deletion (not confused with "content changed"), and no-copy.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.data.workspace.artifact_ref import mint_ref, resolve_ref


def _write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_minting_the_same_path_twice_returns_the_same_ref(tmp_path: Path):
    """Tier 2: the acceptance criterion, verbatim — idempotent minting."""
    target = _write(tmp_path / "report.pptx")
    ref1 = mint_ref(tmp_path, "alice", target)
    ref2 = mint_ref(tmp_path, "alice", target)
    assert ref1 == ref2


def test_normalization_differences_do_not_mint_a_second_ref(tmp_path: Path):
    """Tier 2: the acceptance criterion, verbatim — relative/absolute
    spellings of the same file mint the SAME ref, via the shared
    normalize_ref_path function."""
    target = _write(tmp_path / "sub" / "report.pptx")
    via_absolute = mint_ref(tmp_path, "alice", target)
    via_relative = mint_ref(tmp_path, "alice", "sub/report.pptx")
    assert via_absolute == via_relative


def test_symlink_spelling_does_not_mint_a_second_ref(tmp_path: Path):
    """Tier 2: a symlink to an already-minted file resolves to the same ref."""
    real = _write(tmp_path / "real.txt")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    ref_real = mint_ref(tmp_path, "alice", real)
    ref_link = mint_ref(tmp_path, "alice", link)
    assert ref_real == ref_link


def test_different_agents_get_independent_refs_for_the_same_path(tmp_path: Path):
    """Tier 2: scope is per-agent — two agents minting the SAME path each
    get their own ref (no cross-agent sharing, matching the brief's
    "scope = session/agent" ruling)."""
    target = _write(tmp_path / "report.pptx")
    ref_alice = mint_ref(tmp_path, "alice", target)
    ref_bob = mint_ref(tmp_path, "bob", target)
    assert ref_alice != ref_bob
    assert resolve_ref(tmp_path, "alice", ref_alice) == target.resolve()
    assert resolve_ref(tmp_path, "bob", ref_bob) == target.resolve()


def test_resolve_returns_the_absolute_path(tmp_path: Path):
    """Tier 2: resolve_ref round-trips mint_ref's own path, absolute."""
    target = _write(tmp_path / "report.pptx")
    ref = mint_ref(tmp_path, "alice", target)
    resolved = resolve_ref(tmp_path, "alice", ref)
    assert resolved == target.resolve()
    assert resolved.is_absolute()


def test_unknown_ref_resolves_to_none(tmp_path: Path):
    """Tier 2: a ref that was never minted resolves to None, not an error."""
    assert resolve_ref(tmp_path, "alice", "never-minted") is None


def test_a_deleted_target_becomes_unresolvable(tmp_path: Path):
    """Tier 2: the acceptance criterion, verbatim — the target vanishing
    makes the ref unresolvable (distinct from "content changed", tested
    next)."""
    target = _write(tmp_path / "report.pptx")
    ref = mint_ref(tmp_path, "alice", target)
    target.unlink()
    assert resolve_ref(tmp_path, "alice", ref) is None


def test_a_content_change_does_not_make_the_ref_unresolvable(tmp_path: Path):
    """Tier 2: the acceptance criterion's other half — regenerating the
    SAME path with DIFFERENT bytes still resolves (path identity, not
    content-hash identity); resolve_ref must not confuse "changed" with
    "gone"."""
    target = _write(tmp_path / "report.pptx", b"version one")
    ref = mint_ref(tmp_path, "alice", target)

    target.write_bytes(b"a completely different version two")

    resolved = resolve_ref(tmp_path, "alice", ref)
    assert resolved == target.resolve()
    assert resolved.read_bytes() == b"a completely different version two"


def test_minting_never_copies_the_file(tmp_path: Path):
    """Tier 2: the acceptance criterion, verbatim — mint_ref creates no new
    copy of the artifact. Verified two ways: (a) the only new on-disk
    write is the ref table itself, and (b) the target's own inode is
    unchanged (same file, not a copy with a fresh inode)."""
    target = _write(tmp_path / "report.pptx")
    before_inode = target.stat().st_ino
    before_files = sorted(p for p in tmp_path.rglob("*") if p.is_file())

    mint_ref(tmp_path, "alice", target)

    after_inode = target.stat().st_ino
    after_files = sorted(p for p in tmp_path.rglob("*") if p.is_file())
    new_files = set(after_files) - set(before_files)

    assert before_inode == after_inode
    # The ONLY new file allowed to appear is the ref table itself.
    assert new_files <= {tmp_path / ".reyn" / "cache" / "artifact_refs.jsonl"}


def test_table_is_a_jsonl_manifest_matching_the_4432_spill_manifest_shape(tmp_path: Path):
    """Tier 2: (accept-side, format) one JSON object per line under
    .reyn/cache/ — mirrors #4432's tool-result spill manifest shape, as
    the brief explicitly asked for."""
    target = _write(tmp_path / "report.pptx")
    ref = mint_ref(tmp_path, "alice", target)

    table_path = tmp_path / ".reyn" / "cache" / "artifact_refs.jsonl"
    lines = [line for line in table_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    entries = [json.loads(line) for line in lines]
    (entry,) = [e for e in entries if e["ref"] == ref]
    assert entry["agent"] == "alice"
    assert entry["path"] == str(target.resolve())
