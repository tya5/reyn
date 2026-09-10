"""Tier 2: #6050 — ``MediaStore``'s own eviction now forgets what it
deletes. Both `path.unlink()` call sites (write-time-cap
``_evict_history_content_over_cap``, project-wide
``_evict_cross_session_over_cap``) used to leave a file THIS SAME
process deleted still tracked in ``_history_content_spill_paths``/
``_unspilled_paths`` forever — the set kept asserting the file "is
known" after this store's own eviction removed it. Fixed by collapsing
every ``Path.unlink()`` call in this class into ONE method
(``MediaStore._unlink_tracked``), which fuses the delete with
``self._forget(path)`` so bookkeeping cannot be split from deletion
again (CI-enforced: ``scripts/check_media_store_unlink_single_caller.py``).

Architect's own scoping (issue #6050, tree ``b02492a30``):

- Only ``_history_content_spill_paths`` gets a real bounding subject
  from this fix (the storage cap: cap deletes a file, the set forgets
  it in the SAME call, so the set's own size tracks live files). NOT
  ``_unspilled_paths`` — an un-spilled file is `continue`'d past every
  eviction call site (never a candidate), so it never reaches
  ``_unlink_tracked`` and never gets forgotten either. Separate
  subject (#5896 stage ①, already disclosed there and warned about via
  ``_note_cap_state`` — not this issue's scope, not re-claimed here).
- The read-side consumer population for the "stale set answers about a
  DELETED file" face has NO reachable caller today (architect's own
  ``git grep`` census: all 3 read methods' own candidates come from
  live disk enumeration or a post-read-success path, never a
  since-deleted one) — so this PR does NOT claim "a wrong answer is
  observed today". What IS bound is the set's own SIZE (the ``band``
  axis label this issue carries): without this fix, a long-running
  process's own eviction could only ever GROW these sets, no matter
  how many files it deleted.

Real ``MediaStore`` throughout, real on-disk writes — same idiom as
``tests/data/test_5364_history_content_cap_eviction.py`` (this file's
own established sibling), no fakes. Never asserts on
``_history_content_spill_paths``/``_unspilled_paths`` directly (CLAUDE.md:
no private-state assertions) — only through the public
``is_known_file``/``is_history_content_spill``/``is_unspilled_file``
read surface, matching architect's own explicit instruction.
"""
from __future__ import annotations

import os

from reyn.data.workspace.media_store import (
    MediaStore,
    MediaStoreConfig,
    history_content_root_for,
)


def _bump_all_mtimes_forward(directory) -> None:
    """Same determinism helper this file's own #5364 sibling uses --
    force every existing file further into the past relative to
    whatever gets written next, so eviction order (oldest-mtime-first)
    is deterministic across a fast test loop."""
    for path in directory.rglob("*"):
        if path.is_file():
            st = path.stat()
            os.utime(path, (st.st_atime, st.st_mtime - 1))


def test_eviction_forgets_the_deleted_file_without_the_ladder_ever_running(
    tmp_path,
) -> None:
    """Tier 2: #6050 accept ⑴ (lead-coder's own specified witness) --
    write-time-cap eviction (driven by a normal `save_tool_result` that
    pushes the session over cap) deletes the OLDEST spill and, in that
    SAME call, `is_known_file` stops recognizing it -- WITHOUT the
    `#5939` memory ladder's own `clear_spill_path_cache()` ever running
    (not called anywhere in this test -- the docstring's own claim that
    THIS mechanism, not the ladder, is what forgets).

    - Vacuity guard: `not evicted.exists()` first -- if nothing were
      actually deleted, "is_known_file returns False" would be true of
      an unwritten path too, trivially.
    - Present-sibling guard: `is_known_file(survivor) is True` -- without
      it, a build that always returns False (an empty/broken set) would
      also pass the primary assertion.
    """
    store = MediaStore(
        MediaStoreConfig(history_content_max_bytes=50),
        project_root=tmp_path, agent_name="alice", session_id="main",
    )

    evicted_block = store.save_tool_result(
        "payload number 0 " * 2, mime_type="text/plain", seq=0,
    )
    _bump_all_mtimes_forward(store.history_content_dir)
    survivor_block = store.save_tool_result(
        "payload number 1 " * 2, mime_type="text/plain", seq=1,
    )

    evicted = tmp_path / evicted_block["path"]
    survivor = tmp_path / survivor_block["path"]

    # Vacuity guard: the eviction genuinely happened.
    assert not evicted.exists(), "setup: the older write must actually be evicted"

    # #6050's own subject.
    assert store.is_known_file(evicted) is False, (
        "#6050 REGRESSION: a file this store's OWN eviction just deleted "
        "must stop being 'known' in the SAME call, without the memory "
        "ladder's clear_spill_path_cache() ever running"
    )

    # Present-sibling guard: is_known_file is not just always False.
    assert store.is_known_file(survivor) is True, (
        "the SURVIVING file must still read as known -- otherwise the "
        "primary assertion above would pass even with an empty/broken set"
    )


def test_eviction_forgets_the_deleted_file_through_a_symlinked_agent_directory(
    tmp_path,
) -> None:
    """Tier 2: #6050 accept ⑵ (lead-coder's own specified witness) --
    reproduces architect's own root-cause finding directly: `history_
    content_dir_for` resolves ONLY the shared root
    (`(project_root / cfg.history_content_dir).resolve()`) and then
    APPENDS the agent/session segments WITHOUT re-resolving. If the
    agent segment is itself a symlink (this test's own stand-in for
    macOS's real `/tmp` -> `/private/tmp`, or any other symlink an
    operator's own environment introduces on that path), eviction's own
    `_eviction_order` walks the directory via the UNRESOLVED "alice"
    segment while the recorded set entry was resolved (`abs_path.
    resolve()` at write time) THROUGH the symlink to "alice_real" --
    two textually different `Path` objects naming the same file. A
    naive `self._history_content_spill_paths.discard(path)` using the
    raw (unresolved) eviction-loop `path` would silently no-op here --
    exactly the failure mode issue #6050's own body flagged as a likely
    real-machine-only bug (the suggested fix "add .discard(path) at both
    call sites" was explicitly rejected for this reason).
    """
    root = history_content_root_for(tmp_path, MediaStoreConfig())
    root.mkdir(parents=True, exist_ok=True)
    real_agent_dir = root / "alice_real"
    real_agent_dir.mkdir()
    (root / "alice").symlink_to(real_agent_dir)

    store = MediaStore(
        MediaStoreConfig(history_content_max_bytes=50),
        project_root=tmp_path, agent_name="alice", session_id="main",
    )

    evicted_block = store.save_tool_result(
        "payload number 0 " * 2, mime_type="text/plain", seq=0,
    )
    _bump_all_mtimes_forward(store.history_content_dir)
    survivor_block = store.save_tool_result(
        "payload number 1 " * 2, mime_type="text/plain", seq=1,
    )

    evicted = (tmp_path / evicted_block["path"]).resolve()
    survivor = (tmp_path / survivor_block["path"]).resolve()

    assert not evicted.exists(), "setup: the older write must actually be evicted"
    assert store.is_known_file(evicted) is False, (
        "#6050 REGRESSION: the resolution mismatch through a symlinked "
        "agent directory must not make the discard silently no-op -- "
        "_unlink_tracked/_forget must resolve the SAME way the read "
        "side (is_known_file) does"
    )
    assert store.is_known_file(survivor) is True
