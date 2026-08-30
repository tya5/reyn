"""Tier 2: #5564 — the spill predicate/manifest is "history-content
spill", not "tool-result spill", and never branched on origin (only its
NAME lied about that).

#5514 §7-1 removed the OLD ``role == "tool"`` eligibility restriction
from ``router_loop_driver.py``'s own spill-candidate selection
(``_spill_candidates``/``_spill_fn``) — ANY turn with an inline string
body is a spill candidate now, regardless of role. But the write path
those candidates reach (``RouterHistoryBuffer.spill_turn_content`` →
``MediaStore.save_tool_result``) and the read-side classification
(``MediaStore.is_history_content_spill``, formerly ``is_tool_result_
spill``) were never origin-aware either — this file proves that directly,
driving the REAL production write seam (``spill_turn_content``, not
``save_tool_result`` called by hand) for both a tool-origin and a
user-origin turn.

Accept-side pair (lead-coder review, #5564):
① a spill written from a NON-tool turn classifies exactly like one
  written from a tool turn — no origin-based branch. Written as two
  independent POSITIVE assertions (both paths are ``True``), not an
  equality-of-two-unknowns check — the latter would stay green even
  under an "always returns False" implementation, since both sides would
  be equally (wrongly) False. This file's own ② below is what an
  equality-only ① would have needed anyway: proof the predicate isn't
  just uniformly broken.
② the pre-existing #4381 loop-prevention guard
  (test_4381_read_char_offset_and_spill_guard.py, unchanged file,
  mechanically renamed for #5564) remains green and remains
  strip-falsifiable — this file does not re-implement that guard, it
  only depends on the same sibling suite continuing to pass.

Strip-falsify (performed during review, both directions):
- ``MediaStore.is_history_content_spill``'s own body changed to
  ``return False`` unconditionally: this file's own positive assertions
  (①, above) go RED — ``assert ... is True`` fails on the very first
  path. Restored, both pass again.
- ``op_runtime/file.py``'s own guard call site
  (``ctx.media_store.is_history_content_spill(op.path)``) changed to
  ``if False:``: 2 of ``test_4381_read_char_offset_and_spill_guard.py``'s
  own tests go RED (``assert 'truncated' == 'error'``) — proving ②'s
  claim ("the pre-existing guard remains strip-falsifiable") is not
  merely asserted but actually exercised. Restored, all 7 of that file's
  tests pass again.

Real ``RouterHistoryBuffer`` + real ``MediaStore`` — no mocks (testing
policy, docs/deep-dives/contributing/testing.md).
"""
from __future__ import annotations

from pathlib import Path

from reyn.config.chat import CompactionConfig
from reyn.data.workspace.media_store import MediaStore
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer


def _buffer(media_store: MediaStore) -> RouterHistoryBuffer:
    """A minimal RouterHistoryBuffer — only the fields spill_turn_content
    itself reads (_media_store/_model_fn/_compaction/_events) matter for
    this file; every other constructor param a real Session always
    supplies is irrelevant to the method under test, same posture
    test_session_router_history_slicing.py's own buffer construction
    already uses for its own narrower purpose."""
    return RouterHistoryBuffer(
        history_fn=lambda: [],
        compaction=CompactionConfig(),
        compaction_controller=None,
        model_fn=lambda: "openai/gpt-4o",
        events=None,
        media_store=media_store,
        router_host=None,
        universal_wrappers_enabled=False,
        non_interactive=False,
    )


def test_a_user_origin_spill_classifies_the_same_as_a_tool_origin_spill(
    tmp_path: Path,
) -> None:
    """Tier 2: ① — drives the REAL ``spill_turn_content`` write seam for
    a tool-origin turn (``tool="read_file"``, the shape a real tool
    result carries) and a user-origin turn (``tool="user"``, the shape
    #5564's own fix now passes for a non-tool candidate — see
    ``router_loop_driver.py``'s two real call sites), then asserts
    ``MediaStore.is_history_content_spill`` returns ``True`` for BOTH
    resulting paths — proving the read-side classification does not
    discriminate by origin, with the paths independently confirmed
    positive (not merely equal) so an "always False" implementation
    cannot pass this test."""
    store = MediaStore(project_root=tmp_path, agent_name="test-agent", session_id="test-session")
    buf = _buffer(store)
    huge = "line content here, " * 5000  # forces the offload branch (cap_tokens=1)

    # Part 1: the REAL caller-facing seam (RouterHistoryBuffer.
    # spill_turn_content, what router_loop_driver.py's two real call
    # sites use) must actually make progress for a non-tool ``tool=``
    # value — proving #5564's own origin-honesty fix (passing the turn's
    # role instead of a bare "tool" default) doesn't itself break the
    # spill. Not used for the classification assertion below: this
    # method's own on_offload-less return is a preview STRING, and this
    # module's convention (tool_result_cap.py's own ``on_offload``
    # docstring) is to never re-derive a path by parsing that text.
    tool_preview = buf.spill_turn_content(huge, tool="read_file", seq=1)
    user_preview = buf.spill_turn_content(huge + " (a second, distinct body)", tool="user", seq=2)
    assert tool_preview is not None and user_preview is not None, (
        "both spills must actually offload (a media_store is configured and "
        "cap_tokens=1 forces the branch) -- a None here means this test's own "
        "premise didn't hold, not that origin was rejected"
    )

    # Part 2: the classification assertion itself, driven at
    # save_tool_result (the ONE write call site spill_turn_content
    # itself delegates to — see that method's own docstring) so the
    # written path is read directly off the returned block, never parsed
    # out of a preview string.
    tool_block = store.save_tool_result("distinct probe content A", tool="probe-tool")
    user_block = store.save_tool_result("distinct probe content B", tool="user")

    assert store.is_history_content_spill(tool_block["path"]) is True
    assert store.is_history_content_spill(user_block["path"]) is True


def test_an_ordinary_non_spilled_path_still_classifies_false_regardless_of_name(
    tmp_path: Path,
) -> None:
    """Tier 2: deny-side pair to the positive test above — a path this
    store never wrote (whatever name it has, including one that LOOKS
    like it could be user-authored) must classify False. Without this,
    ① alone could pass a broken "classify by filename substring" reading
    of "origin-agnostic" that isn't actually reading the manifest at
    all."""
    store = MediaStore(project_root=tmp_path, agent_name="test-agent", session_id="test-session")
    ordinary = tmp_path / "user-notes.txt"
    ordinary.write_text("never written via save_tool_result")

    assert store.is_history_content_spill(str(ordinary)) is False
