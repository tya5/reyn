"""Tier 2: #5603 — ``reyn doctor``'s litellm compat-patch status line.

D-1 (doctor's own rule, module docstring): measure, don't assert — this
check must read the REAL applied-state flag reyn's own patch functions
set on the real litellm class objects, never restate that the patch
module merely exists (which would still be true even if the patch itself
silently failed to apply — the exact #5568 incident this whole issue
exists to prevent recurring).

Real ``ensure_litellm_ready()``/real litellm import throughout (this
check's own whole point is to trigger the SAME chokepoint a real call
uses) — no mock, no stand-in class.
"""
from __future__ import annotations

from reyn.interfaces.cli.commands.doctor import _print_litellm_patch_status


def test_both_patches_report_applied_after_a_real_import(capsys) -> None:
    """Tier 2: #5603 accept — a real ``ensure_litellm_ready()`` call
    (triggered by this function itself) applies both patches; the doctor
    line reads their own real class-attribute flags back and reports
    both applied."""
    _print_litellm_patch_status()
    out = capsys.readouterr().out

    assert "✓ applied: stream_chunk_recovery (A)" in out, out
    assert "✓ applied: overflow_diagnosis (B)" in out, out


def test_measures_the_real_class_attribute_not_a_restated_declaration(capsys) -> None:
    """Tier 2: #5603 accept — the reported state is read directly off the
    real litellm class objects, not derived from "the patch module
    imports successfully" (D-1's own distinction, and the #5568
    incident's own root cause: a broken import can leave the patch
    module unusable while the REST of the process still starts fine —
    only reading the actual flag distinguishes the two)."""
    from litellm.completion_extras.litellm_responses_transformation import handler as H
    from litellm.llms.chatgpt.responses import transformation as T

    _print_litellm_patch_status()

    assert getattr(H.ResponsesToCompletionBridgeHandler, "_reyn_5603_patched", False) is True
    assert getattr(T.ChatGPTResponsesAPIConfig, "_reyn_5603b_patched", False) is True

    out = capsys.readouterr().out
    assert "✓ applied: stream_chunk_recovery (A)" in out
    assert "✓ applied: overflow_diagnosis (B)" in out
