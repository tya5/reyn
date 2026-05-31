"""Tier 2: OS invariant — chat tool-result cap is offload-based + by-construction bounded (#1128).

The size-axis fix for conversation dead-end #1: an oversized chat tool result is
OFFLOADED (full body stored, lossless, restorable) and replaced inline with a
bounded preview whose estimated tokens are ``<= cap_tokens``. Because
``cap_tokens = min(FIXED_CEIL, floor(0.5·effective_trigger)) < effective_trigger``,
the capped result is single-turn compactable on every model — so retry_loop's
shrink can always fold it (closes the dead-end).

These pin the helper's contract with the real offload store + real
``MediaStore.read_tool_result`` read-back (no mocks):
  - under-cap content is identity (no offload),
  - over-cap content is offloaded + the inline preview is ``<= cap_tokens`` (the
    by-construction bound, across small + large caps),
  - the full body reads back losslessly via ``read_tool_result`` (the
    no-lossy-truncate guarantee — body is never discarded or raw-``[:N]``-cut),
  - cap_tokens<=0 disables the cap.

``use_chars4=True`` matches the chars//4 estimator deterministically offline.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.services.tool_result_cap import (
    MAX_TOOL_RESULT_INLINE_BYTES,
    cap_tool_result_content,
)
from reyn.services.compaction.engine import estimate_tokens
from reyn.workspace.media_store import MediaStore

_MODEL = "gpt-4o"


def _store_dir(tmp_path: Path) -> Path:
    # Mirror MediaStore's default tool_results_dir layout so read_tool_result
    # (project-relative under tool_results_dir) resolves the body we wrote.
    return tmp_path / ".reyn" / "tool-results"


def test_under_cap_content_is_identity(tmp_path: Path) -> None:
    """Tier 2: a result within cap_tokens is returned unchanged (no offload)."""
    content = "small tool result"
    out = cap_tool_result_content(
        content, cap_tokens=2048, model=_MODEL,
        store_dir=_store_dir(tmp_path), use_chars4=True,
    )
    assert out == content
    # Nothing written when under cap.
    assert not _store_dir(tmp_path).exists() or not any(_store_dir(tmp_path).iterdir())


@pytest.mark.parametrize("cap_tokens", [256, 1024, 4096])
def test_over_cap_preview_is_within_cap_tokens(tmp_path: Path, cap_tokens: int) -> None:
    """Tier 2: an oversized result's inline preview is <= cap_tokens (by-construction).

    Holds across small + large caps — the dead-end-#1 closure is model-independent.
    """
    content = "X" * 400_000  # ~100k tokens (chars//4) — far over any cap
    out = cap_tool_result_content(
        content, cap_tokens=cap_tokens, model=_MODEL,
        store_dir=_store_dir(tmp_path), project_root=tmp_path, use_chars4=True,
    )
    assert out != content, "oversized content must be offloaded, not returned raw"
    assert estimate_tokens(out, _MODEL, use_chars4=True) <= cap_tokens, (
        "the offloaded inline preview must itself fit cap_tokens so it is "
        "single-turn compactable (the by-construction dead-end-#1 bound)"
    )
    assert len(out) <= MAX_TOOL_RESULT_INLINE_BYTES
    assert "_offload_ref" in out


def test_offloaded_body_reads_back_lossless(tmp_path: Path) -> None:
    """Tier 2: the full body is recoverable via MediaStore.read_tool_result (lossless).

    The no-lossy-truncate guarantee: the body is stored, never discarded or
    raw-truncated. The inline preview is just a bounded pointer.
    """
    import json

    content = "LINE\n" * 50_000  # large, distinctive
    out = cap_tool_result_content(
        content, cap_tokens=512, model=_MODEL,
        store_dir=_store_dir(tmp_path), project_root=tmp_path, use_chars4=True,
    )
    ref = json.loads(out)["_offload_ref"]

    store = MediaStore(project_root=tmp_path)
    body, found = store.read_tool_result(ref)
    assert found, f"read_tool_result could not locate the offloaded body at {ref!r}"
    assert body == content, "read-back must return the full original body (lossless)"


def test_cap_disabled_when_zero(tmp_path: Path) -> None:
    """Tier 2: cap_tokens<=0 disables the cap (identity, no offload)."""
    content = "Y" * 100_000
    out = cap_tool_result_content(
        content, cap_tokens=0, model=_MODEL,
        store_dir=_store_dir(tmp_path), use_chars4=True,
    )
    assert out == content
