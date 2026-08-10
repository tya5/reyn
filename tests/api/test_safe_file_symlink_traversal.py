"""Tier 2: safe/file resists symlink-traversal escape (path-safety hardening).

`api/safe/file._is_under` gated with `os.path.abspath` (resolves `..` but NOT
symlinks), so a symlink inside an allowed dir pointing outside escaped the gate —
read/write OUTSIDE the declared root (reproduced: read a secret + overwrite an
arbitrary outside file). Fixed with a realpath gate (resolves symlinks at check
time, incl. parent-dir symlinks) + `O_NOFOLLOW` on the opens (final-component
TOCTOU guard).

Policy: real filesystem + real symlinks + real `safe.file` (its permission
context is the seam, set directly) — no mocks. Tier line first.
"""
from __future__ import annotations

import os

import pytest

import reyn.api.safe.file as sf


@pytest.fixture
def ctx(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sf._set_permission_context(read_paths=[str(allowed)], write_paths=[str(allowed)])
    return allowed, outside


def test_read_via_symlink_to_outside_blocked(ctx):
    """Tier 2: a symlink inside an allowed dir → outside is BLOCKED on read (the
    realpath gate resolves the symlink to its outside target → denied)."""
    allowed, outside = ctx
    secret = outside / "secret.txt"
    secret.write_text("OUTSIDE-SECRET")
    link = allowed / "link"
    os.symlink(secret, link)
    with pytest.raises(PermissionError):
        sf.read(str(link))


def test_write_via_symlink_to_outside_blocked(ctx):
    """Tier 2: writing through an allowed-dir symlink → outside is BLOCKED (the
    reproduced arbitrary-outside-file overwrite); the target stays unchanged."""
    allowed, outside = ctx
    victim = outside / "victim.txt"
    victim.write_text("ORIGINAL")
    link = allowed / "wlink"
    os.symlink(victim, link)
    with pytest.raises(PermissionError):
        sf.write(str(link), "PWNED-VIA-SYMLINK")
    assert victim.read_text() == "ORIGINAL"


def test_read_via_parent_dir_symlink_blocked(ctx):
    """Tier 2: a symlink in a PARENT component → outside is BLOCKED — realpath
    resolves the whole path (O_NOFOLLOW alone guards only the final component)."""
    allowed, outside = ctx
    sub = outside / "sub"
    sub.mkdir()
    (sub / "f.txt").write_text("PARENT-ESCAPE")
    dlink = allowed / "dlink"
    os.symlink(sub, dlink)
    with pytest.raises(PermissionError):
        sf.read(str(dlink / "f.txt"))


def test_within_allowed_final_symlink_refused_by_nofollow(ctx):
    """Tier 2: the O_NOFOLLOW layer refuses a final-component symlink even when its
    target is WITHIN the allowed root (no symlink-following at all — the atomic
    TOCTOU-safe behavior; intended post-fix stricture)."""
    allowed, _outside = ctx
    real = allowed / "real.txt"
    real.write_text("INNER")
    link = allowed / "innerlink"
    os.symlink(real, link)
    with pytest.raises(OSError):  # ELOOP from O_NOFOLLOW (not a PermissionError)
        sf.read(str(link))


def test_legit_non_symlink_paths_still_work(ctx):
    """Tier 2: (regression) legit non-symlink reads/writes under the allowed root
    are unaffected by the hardening."""
    allowed, _outside = ctx
    p = allowed / "ok.txt"
    sf.write(str(p), "hello")
    assert sf.read(str(p)) == "hello"
