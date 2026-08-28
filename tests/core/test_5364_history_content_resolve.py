"""Tier 1: #5364 §1.2 — the tool-result history-content resolver's own
truth table (a pure function; all four branches directly, plus the
orthogonality owner named explicitly)."""
from __future__ import annotations

from reyn.core.offload.history_content_resolve import HistoryContentEntry, resolve


def test_not_spilled_and_file_present_resolves_inline() -> None:
    """Tier 1: the common case — a small tool result, never offloaded,
    its backing file (§1.1 "A": always written) still exists."""
    entry = HistoryContentEntry(spilled=False, content="the original body", ref="some/path.txt")

    result = resolve(entry, file_exists=lambda ref: True)

    assert result.kind == "inline"
    assert result.value == "the original body"


def test_spilled_and_file_present_resolves_ref() -> None:
    """Tier 1: an offloaded tool result whose backing file is still
    there — resolves to the path, not the (possibly discarded) inline
    content."""
    entry = HistoryContentEntry(spilled=True, content="stale/unused", ref="spill/path.txt")

    result = resolve(entry, file_exists=lambda ref: True)

    assert result.kind == "ref"
    assert result.value == "spill/path.txt"


def test_not_spilled_but_file_missing_resolves_lost() -> None:
    """Tier 1: orthogonality (owner ruling, #5364: "spill/lost 判定は直
    行性を持たせるべき") — an UNSPILLED entry's backing file can still be
    gone (GC'd, or never persisted at all — #5364 §1.5's two reasons),
    and that alone is enough to resolve ``lost``, independent of
    ``spilled``."""
    entry = HistoryContentEntry(spilled=False, content="never mattered", ref="gone/path.txt")

    result = resolve(entry, file_exists=lambda ref: False)

    assert result.kind == "lost"
    assert result.value == "gone/path.txt"


def test_spilled_and_file_missing_resolves_lost() -> None:
    """Tier 1: the fourth cell of the 2x2 table — spilled AND gone."""
    entry = HistoryContentEntry(spilled=True, content="irrelevant", ref="also/gone.txt")

    result = resolve(entry, file_exists=lambda ref: False)

    assert result.kind == "lost"
    assert result.value == "also/gone.txt"


def test_file_exists_is_called_with_this_entrys_own_ref() -> None:
    """Tier 1: the resolver checks THIS entry's ref, not some other path
    — a resolver that hardcoded or mismatched paths would still pass the
    four branch tests above (they all resolve the SAME entry's own ref)
    but fail this one, which uses distinguishable refs per call."""
    seen: list[str] = []

    def _file_exists(ref: str) -> bool:
        seen.append(ref)
        return True

    resolve(HistoryContentEntry(spilled=False, content="x", ref="entry-a.txt"), _file_exists)
    resolve(HistoryContentEntry(spilled=True, content="y", ref="entry-b.txt"), _file_exists)

    assert seen == ["entry-a.txt", "entry-b.txt"]
