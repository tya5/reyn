"""Tier 2: #5364 §1.2 — the tool-result history-content resolver's own
truth table (a pure function; every cell of the module's table directly,
plus the orthogonality owner named explicitly). #5896 widened the table
from 2×2 to the four un-spilled shapes the return-time "A" path produces
(no ref / ref + body / ref + no body, present / ref + no body, lost)."""
from __future__ import annotations

import pytest

from reyn.core.offload.history_content_resolve import HistoryContentEntry, resolve


def _never_read(ref: str) -> str:
    raise AssertionError(f"read_text must not be called for this cell (ref={ref!r})")


def _never_exists(ref: str) -> bool:
    raise AssertionError(f"file_exists must not be called for this cell (ref={ref!r})")


def test_not_spilled_and_no_ref_resolves_inline_without_touching_disk() -> None:
    """Tier 2: a pre-#5896 row (body inline, no ref was ever minted) or a
    #5364 §1.5 write-refused row — self-sufficient; neither injected
    callable is consulted (#5506, architect ruling: ``spilled`` first,
    and an unspilled entry with nothing to look up never asks)."""
    entry = HistoryContentEntry(spilled=False, content="the original body", ref="")

    result = resolve(entry, file_exists=_never_exists, read_text=_never_read)

    assert result.kind == "inline"
    assert result.value == "the original body"


def test_not_spilled_with_ref_and_body_resolves_inline_from_the_body() -> None:
    """Tier 2: #5896 — a row that already holds its body (the resident
    ``ChatMessage`` ``RouterLoop.feedback`` appended, or one hydrated at
    parse time) is the LIVE cache: the file is the durable copy, never
    re-read while a body is at hand — "生きている process は disk を読み
    直さない" (architect, #5896). Neither callable is consulted, so a
    file deleted under a live process changes nothing it shows."""
    entry = HistoryContentEntry(spilled=False, content="still here", ref="some/path.txt")

    result = resolve(entry, file_exists=_never_exists, read_text=_never_read)

    assert result.kind == "inline"
    assert result.value == "still here"


def test_not_spilled_with_ref_and_no_body_reads_the_file() -> None:
    """Tier 2: #5896 — a ``history.jsonl`` row read back after a restart
    carries no body (``chat_message.history_record`` dropped it); the
    body comes from ITS OWN ref through the injected reader."""
    entry = HistoryContentEntry(spilled=False, content="", ref="hist/row.txt")
    read: list[str] = []

    def _read_text(ref: str) -> str:
        read.append(ref)
        return "body from the file"

    result = resolve(entry, file_exists=lambda ref: True, read_text=_read_text)

    assert result.kind == "inline"
    assert result.value == "body from the file"
    assert read == ["hist/row.txt"]


def test_not_spilled_with_ref_no_body_and_file_missing_resolves_lost() -> None:
    """Tier 2: #5896 — the one cell where an un-spilled row is lost: no
    body on the row, and the only durable copy is gone. Reyn's own GC
    never selects an un-spilled file (stage ①), so reaching this cell
    means something outside reyn removed it — the resolver only says
    THAT it is lost; the caller derives the reason."""
    entry = HistoryContentEntry(spilled=False, content="", ref="gone/path.txt")

    result = resolve(entry, file_exists=lambda ref: False, read_text=_never_read)

    assert result.kind == "lost"
    assert result.value == "gone/path.txt"


def test_not_spilled_with_ref_no_body_and_no_reader_is_a_caller_bug() -> None:
    """Tier 2: #5896 — a caller that can reach the read cell without
    supplying a reader gets a ``TypeError``, never a silent ``lost`` (a
    present file resolving as lost would be the resolver lying about the
    filesystem to cover for its caller)."""
    entry = HistoryContentEntry(spilled=False, content="", ref="hist/row.txt")

    with pytest.raises(TypeError):
        resolve(entry, file_exists=lambda ref: True)


def test_spilled_and_file_present_resolves_ref() -> None:
    """Tier 2: an offloaded tool result whose backing file is still
    there — resolves to the path, not the (possibly discarded) inline
    content. ``read_text`` is never consulted on the spilled side."""
    entry = HistoryContentEntry(spilled=True, content="stale/unused", ref="spill/path.txt")

    result = resolve(entry, file_exists=lambda ref: True, read_text=_never_read)

    assert result.kind == "ref"
    assert result.value == "spill/path.txt"


def test_spilled_and_file_missing_resolves_lost() -> None:
    """Tier 2: the spilled-and-gone cell."""
    entry = HistoryContentEntry(spilled=True, content="irrelevant", ref="also/gone.txt")

    result = resolve(entry, file_exists=lambda ref: False, read_text=_never_read)

    assert result.kind == "lost"
    assert result.value == "also/gone.txt"


def test_file_exists_is_called_with_this_entrys_own_ref() -> None:
    """Tier 2: the resolver checks THIS entry's ref, not some other path
    — a resolver that hardcoded or mismatched paths would still pass the
    spilled branch tests above (they all resolve the SAME entry's own
    ref) but fail this one, which uses distinguishable refs per call, on
    both sides of the table that consult the filesystem (a spilled entry
    and an un-spilled, body-less one)."""
    seen: list[str] = []

    def _file_exists(ref: str) -> bool:
        seen.append(ref)
        return True

    resolve(HistoryContentEntry(spilled=True, content="x", ref="entry-a.txt"), _file_exists)
    resolve(HistoryContentEntry(spilled=True, content="y", ref="entry-b.txt"), _file_exists)
    resolve(
        HistoryContentEntry(spilled=False, content="", ref="entry-c.txt"),
        _file_exists, read_text=lambda ref: "",
    )

    assert seen == ["entry-a.txt", "entry-b.txt", "entry-c.txt"]
