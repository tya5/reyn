"""Tier 2: /answer exposes an id-prefix completer (Wave-11 C#3).

Pinned:
  - ``_intervention_id_completer`` returns active intervention IDs
  - Past-first-whitespace input returns empty (= user is typing
    the answer body, no longer the id)
  - Defensive: no attribute → empty completion
  - ``/answer`` has the completer wired
"""
from __future__ import annotations

import sys

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _StubSource:
    """Minimal ``CompletionSourceSnapshot``-shaped stub (#5044) — only
    carries the ONE field this completer reads."""

    def __init__(self, active_intervention_ids=()):
        self.active_intervention_ids = tuple(active_intervention_ids)


def test_intervention_id_completer_returns_active_ids() -> None:
    """Tier 2: empty arg_partial returns all active intervention IDs."""
    from reyn.interfaces.slash.chat import _intervention_id_completer

    source = _StubSource(active_intervention_ids=["iv-aaa", "iv-bbb"])
    out = _intervention_id_completer(source, "")
    assert set(out) == {"iv-aaa", "iv-bbb"}


def test_intervention_id_completer_filters_by_prefix() -> None:
    """Tier 2: prefix filter narrows."""
    from reyn.interfaces.slash.chat import _intervention_id_completer

    source = _StubSource(active_intervention_ids=["iv-aaa", "iv-bbb", "zz-ccc"])
    assert set(_intervention_id_completer(source, "iv-")) == {"iv-aaa", "iv-bbb"}
    assert _intervention_id_completer(source, "zz") == ["zz-ccc"]


def test_intervention_id_completer_past_first_space_empty() -> None:
    """Tier 2: after the user types past the id+space, the completer goes silent.

    ``/answer <id-prefix> <text>`` — once the user has typed past
    the first whitespace they're writing the answer body, not the
    id. Returning [] lets the picker fall back to plain hint mode.
    """
    from reyn.interfaces.slash.chat import _intervention_id_completer

    source = _StubSource(active_intervention_ids=["iv-aaa"])
    assert _intervention_id_completer(source, "iv-aaa hello") == []
    assert _intervention_id_completer(source, "iv- text") == []


def test_intervention_id_completer_no_source_returns_empty() -> None:
    """Tier 2: a source without ``active_intervention_ids`` (or ``None``
    itself) returns empty (defensive)."""
    from reyn.interfaces.slash.chat import _intervention_id_completer

    class _Bare:
        pass

    assert _intervention_id_completer(_Bare(), "") == []
    assert _intervention_id_completer(None, "") == []


def test_answer_slash_has_completer_registered() -> None:
    """Tier 2: ``/answer`` registers ``_intervention_id_completer``."""
    from reyn.interfaces.slash import REGISTRY
    from reyn.interfaces.slash.chat import _intervention_id_completer

    cmd = REGISTRY.get("answer")
    assert cmd is not None
    assert cmd.completer is _intervention_id_completer
