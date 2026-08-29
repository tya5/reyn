"""Tier 2: #5364 §1.2 — the tool-result history-content resolver's own
truth table (a pure function; all four branches directly, plus the
orthogonality owner named explicitly)."""
from __future__ import annotations

from reyn.core.offload.history_content_resolve import HistoryContentEntry, resolve


def test_not_spilled_and_file_present_resolves_inline() -> None:
    """Tier 2: the common case — a small tool result, never offloaded,
    its backing file (§1.1 "A": always written) still exists."""
    entry = HistoryContentEntry(spilled=False, content="the original body", ref="some/path.txt")

    result = resolve(entry, file_exists=lambda ref: True)

    assert result.kind == "inline"
    assert result.value == "the original body"


def test_spilled_and_file_present_resolves_ref() -> None:
    """Tier 2: an offloaded tool result whose backing file is still
    there — resolves to the path, not the (possibly discarded) inline
    content."""
    entry = HistoryContentEntry(spilled=True, content="stale/unused", ref="spill/path.txt")

    result = resolve(entry, file_exists=lambda ref: True)

    assert result.kind == "ref"
    assert result.value == "spill/path.txt"


def test_not_spilled_and_file_missing_still_resolves_inline() -> None:
    """Tier 2: #5506 (architect ruling, quoted verbatim) — "分岐順序を直
    す（``spilled`` を先に見る）。表の当該行は ``not spilled | file lost →
    inline`` に。" This cell was never reachable through any real caller
    (``router_history_buffer.py`` already short-circuits an unspilled
    entry before calling ``resolve`` at all — verified, #5506: its only
    call site hardcodes ``spilled=True`` and returns early on
    ``not meta.get(SPILLED_META_KEY)``), which is exactly how a second,
    wrong copy of this cell (the module's own prose said file existence
    is IRRELEVANT for an unspilled entry; the table it used to implement
    said the opposite) survived undetected inside code that calls itself
    "the ONE place this 3-way branch is written." An unspilled entry's
    content is self-sufficient regardless of whether its ``ref`` — set or
    not — points at a file that exists."""
    entry = HistoryContentEntry(spilled=False, content="still here", ref="gone/path.txt")

    result = resolve(entry, file_exists=lambda ref: False)

    assert result.kind == "inline"
    assert result.value == "still here"


def test_spilled_and_file_missing_resolves_lost() -> None:
    """Tier 2: the fourth cell of the 2x2 table — spilled AND gone."""
    entry = HistoryContentEntry(spilled=True, content="irrelevant", ref="also/gone.txt")

    result = resolve(entry, file_exists=lambda ref: False)

    assert result.kind == "lost"
    assert result.value == "also/gone.txt"


def test_file_exists_is_called_with_this_entrys_own_ref() -> None:
    """Tier 2: the resolver checks THIS entry's ref, not some other path
    — a resolver that hardcoded or mismatched paths would still pass the
    spilled branch tests above (they all resolve the SAME entry's own
    ref) but fail this one, which uses distinguishable refs per call.
    Both entries here are spilled (#5506: an unspilled entry never calls
    ``file_exists`` at all — see
    ``test_not_spilled_and_file_missing_still_resolves_inline`` above —
    so this test's own subject, THIS entry's ref reaching the callback,
    can only be witnessed on the spilled path)."""
    seen: list[str] = []

    def _file_exists(ref: str) -> bool:
        seen.append(ref)
        return True

    resolve(HistoryContentEntry(spilled=True, content="x", ref="entry-a.txt"), _file_exists)
    resolve(HistoryContentEntry(spilled=True, content="y", ref="entry-b.txt"), _file_exists)

    assert seen == ["entry-a.txt", "entry-b.txt"]
