"""Tier 2: /cost-inline + /docs-filter slash — _normalise helper + sentinel paths.

Both commands emit a sentinel OutboxMessage that the TUI intercepts; the sentinel
kind and text are the observable contract.  /cost-inline also has a pure _normalise
helper that maps raw args to ('on'|'off'|'').
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.cost_inline import _normalise, cost_inline_cmd
from reyn.interfaces.slash.docs_filter import docs_filter_cmd
from reyn.runtime.outbox import OutboxMessage

# ── stub ───────────────────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self) -> None:
        self._outbox: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self._outbox.append(msg)

    def outbox_kinds(self) -> list[str]:
        return [m.kind for m in self._outbox]

    def last_text(self) -> str:
        return self._outbox[-1].text if self._outbox else ""


# ── _normalise ─────────────────────────────────────────────────────────────


def test_normalise_on() -> None:
    """Tier 2: 'on' → 'on'."""
    assert _normalise("on") == "on"


def test_normalise_off() -> None:
    """Tier 2: 'off' → 'off'."""
    assert _normalise("off") == "off"


def test_normalise_empty_is_toggle() -> None:
    """Tier 2: empty string → '' (= toggle)."""
    assert _normalise("") == ""


def test_normalise_toggle_keyword_is_toggle() -> None:
    """Tier 2: 'toggle' → '' (explicit toggle keyword)."""
    assert _normalise("toggle") == ""


def test_normalise_unknown_falls_back_to_toggle() -> None:
    """Tier 2: unrecognised args (e.g. 'yes') → '' (toggle fallback)."""
    assert _normalise("yes") == ""
    assert _normalise("enable") == ""


def test_normalise_case_insensitive() -> None:
    """Tier 2: 'ON' / 'OFF' (upper-case) accepted via lower()."""
    assert _normalise("ON") == "on"
    assert _normalise("OFF") == "off"


# ── /cost-inline handler ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cost_inline_emits_sentinel() -> None:
    """Tier 2: cost_inline_cmd emits a __cost_inline_toggle__ sentinel."""
    session = _FakeSession()
    await cost_inline_cmd(session, "")  # type: ignore[arg-type]
    assert "__cost_inline_toggle__" in session.outbox_kinds()


@pytest.mark.asyncio
async def test_cost_inline_toggle_text_is_empty_for_no_args() -> None:
    """Tier 2: no args → sentinel text is '' (toggle)."""
    session = _FakeSession()
    await cost_inline_cmd(session, "")  # type: ignore[arg-type]
    assert session.last_text() == ""


@pytest.mark.asyncio
async def test_cost_inline_on_arg_sets_sentinel_text() -> None:
    """Tier 2: 'on' arg → sentinel text is 'on'."""
    session = _FakeSession()
    await cost_inline_cmd(session, "on")  # type: ignore[arg-type]
    assert session.last_text() == "on"


@pytest.mark.asyncio
async def test_cost_inline_off_arg_sets_sentinel_text() -> None:
    """Tier 2: 'off' arg → sentinel text is 'off'."""
    session = _FakeSession()
    await cost_inline_cmd(session, "off")  # type: ignore[arg-type]
    assert session.last_text() == "off"


# ── /docs-filter handler ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_docs_filter_emits_sentinel() -> None:
    """Tier 2: docs_filter_cmd emits a __docs_filter__ sentinel."""
    session = _FakeSession()
    await docs_filter_cmd(session, "")  # type: ignore[arg-type]
    assert "__docs_filter__" in session.outbox_kinds()


@pytest.mark.asyncio
async def test_docs_filter_empty_args_clears_filter() -> None:
    """Tier 2: empty args → sentinel text '' (clear the filter)."""
    session = _FakeSession()
    await docs_filter_cmd(session, "")  # type: ignore[arg-type]
    assert session.last_text() == ""


@pytest.mark.asyncio
async def test_docs_filter_substring_passes_through() -> None:
    """Tier 2: non-empty args → sentinel text is the stripped substring."""
    session = _FakeSession()
    await docs_filter_cmd(session, "  workspace  ")  # type: ignore[arg-type]
    assert session.last_text() == "workspace"
