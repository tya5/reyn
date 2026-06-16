"""Tier 2: picker hint sweep — Wave-12 T2-1 (Topic A #3/#4/#7/#8).

4 slash commands whose picker hints underspecified behavior:
  /pending (A#3) — added usage; sub-command vocab now visible in hint
  /agent   (A#4) — summary now mentions rm escape hatch
  /image   (A#7) — summary now lists supported extensions
  /docs-filter (A#8) — summary now names the target tab + Ctrl+B route

All assertions use the REGISTRY public surface (cmd.usage / cmd.summary).
No MagicMock / patch per testing policy.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_pending_usage_surfaces_subcommand_vocab() -> None:
    """Tier 2: /pending usage exposes list/discard/claim in the picker hint."""
    from reyn.slash import REGISTRY

    cmd = REGISTRY.get("pending")
    assert cmd is not None, "/pending not in registry"
    assert cmd.usage == "/pending [list|discard <id>|claim <id>]", (
        f"/pending usage mismatch: got {cmd.usage!r}"
    )


def test_agent_summary_mentions_rm_escape_hatch() -> None:
    """Tier 2: /agent summary tells the user where rm lives."""
    from reyn.slash import REGISTRY

    cmd = REGISTRY.get("agent")
    assert cmd is not None, "/agent not in registry"
    assert "rm via `reyn agent rm`" in cmd.summary, (
        f"/agent summary missing rm escape hatch: {cmd.summary!r}"
    )


def test_image_summary_lists_supported_extensions() -> None:
    """Tier 2: /image summary surfaces png and webp without pinning exact wording."""
    from reyn.slash import REGISTRY

    cmd = REGISTRY.get("image")
    assert cmd is not None, "/image not in registry"
    assert "png" in cmd.summary, (
        f"/image summary missing 'png': {cmd.summary!r}"
    )
    assert "webp" in cmd.summary, (
        f"/image summary missing 'webp': {cmd.summary!r}"
    )


def test_docs_filter_summary_names_tab_and_keybinding() -> None:
    """Tier 2: /docs-filter summary surfaces Ctrl+B route and Docs tab name."""
    from reyn.slash import REGISTRY

    cmd = REGISTRY.get("docs-filter")
    assert cmd is not None, "/docs-filter not in registry"
    assert "Ctrl+B" in cmd.summary, (
        f"/docs-filter summary missing 'Ctrl+B': {cmd.summary!r}"
    )
    assert "Docs" in cmd.summary, (
        f"/docs-filter summary missing 'Docs': {cmd.summary!r}"
    )
