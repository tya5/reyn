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
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _StubSession:
    """Minimal session stub — only carries what the completers read."""

    def __init__(self, interventions=None):
        self._interventions = _StubInterventionRegistry(interventions or [])


class _StubInterventionRegistry:
    def __init__(self, ivs):
        self._ivs = ivs

    def list_active(self):
        return self._ivs


class _StubIntervention:
    def __init__(self, iid):
        self.id = iid


def test_intervention_id_completer_returns_active_ids() -> None:
    """Tier 2: empty arg_partial returns all active intervention IDs."""
    from reyn.interfaces.slash.chat import _intervention_id_completer

    session = _StubSession(interventions=[
        _StubIntervention("iv-aaa"),
        _StubIntervention("iv-bbb"),
    ])
    out = _intervention_id_completer(session, "")
    assert set(out) == {"iv-aaa", "iv-bbb"}


def test_intervention_id_completer_filters_by_prefix() -> None:
    """Tier 2: prefix filter narrows."""
    from reyn.interfaces.slash.chat import _intervention_id_completer

    session = _StubSession(interventions=[
        _StubIntervention("iv-aaa"),
        _StubIntervention("iv-bbb"),
        _StubIntervention("zz-ccc"),
    ])
    assert set(_intervention_id_completer(session, "iv-")) == {"iv-aaa", "iv-bbb"}
    assert _intervention_id_completer(session, "zz") == ["zz-ccc"]


def test_intervention_id_completer_past_first_space_empty() -> None:
    """Tier 2: after the user types past the id+space, the completer goes silent.

    ``/answer <id-prefix> <text>`` — once the user has typed past
    the first whitespace they're writing the answer body, not the
    id. Returning [] lets the picker fall back to plain hint mode.
    """
    from reyn.interfaces.slash.chat import _intervention_id_completer

    session = _StubSession(interventions=[_StubIntervention("iv-aaa")])
    assert _intervention_id_completer(session, "iv-aaa hello") == []
    assert _intervention_id_completer(session, "iv- text") == []


def test_intervention_id_completer_no_registry_returns_empty() -> None:
    """Tier 2: session without ``_interventions`` returns empty (defensive)."""
    from reyn.interfaces.slash.chat import _intervention_id_completer

    class _Bare:
        pass

    assert _intervention_id_completer(_Bare(), "") == []


def test_answer_slash_has_completer_registered() -> None:
    """Tier 2: ``/answer`` registers ``_intervention_id_completer``."""
    from reyn.interfaces.slash import REGISTRY
    from reyn.interfaces.slash.chat import _intervention_id_completer

    cmd = REGISTRY.get("answer")
    assert cmd is not None
    assert cmd.completer is _intervention_id_completer
