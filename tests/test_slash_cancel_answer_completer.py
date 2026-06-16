"""Tier 2: /cancel and /answer expose id-prefix completers (Wave-11 C#3).

Wave-11 finding C#3. Before this PR, ``/cancel <id-prefix>`` and
``/answer <id-prefix> <text>`` relied on the user typing run_id /
intervention_id by hand or scrolling ``/list`` output to find
them. Both commands already enumerate IDs from
``session.running_skills`` / ``session._interventions.list_active()``;
exposing those as picker hint completers gives Tab-recall parity
with ``/attach`` which already has an agent-name completer.

Pinned:
  - ``_running_run_id_completer`` returns running.keys() when
    arg_partial is empty
  - Prefix filter narrows the list
  - Defensive: empty list / no attribute → empty completion
  - ``_intervention_id_completer`` returns active intervention IDs
  - Past-first-whitespace input returns empty (= user is typing
    the answer body, no longer the id)
  - Both slash commands have the completer wired
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _StubSession:
    """Minimal session stub — only carries what the completers read."""

    def __init__(self, run_ids=None, interventions=None):
        self.running_skills = {rid: object() for rid in (run_ids or [])}
        self._interventions = _StubInterventionRegistry(interventions or [])


class _StubInterventionRegistry:
    def __init__(self, ivs):
        self._ivs = ivs

    def list_active(self):
        return self._ivs


class _StubIntervention:
    def __init__(self, iid):
        self.id = iid


def test_running_run_id_completer_returns_keys_when_empty_partial() -> None:
    """Tier 2: empty arg_partial returns all running ``run_id`` keys."""
    from reyn.slash.chat import _running_run_id_completer

    session = _StubSession(run_ids=["abc123", "def456", "ghi789"])
    out = _running_run_id_completer(session, "")
    assert set(out) == {"abc123", "def456", "ghi789"}


def test_running_run_id_completer_filters_by_prefix() -> None:
    """Tier 2: prefix narrows the completion list."""
    from reyn.slash.chat import _running_run_id_completer

    session = _StubSession(run_ids=["abc123", "abc999", "def456"])
    assert set(_running_run_id_completer(session, "abc")) == {"abc123", "abc999"}
    assert _running_run_id_completer(session, "def") == ["def456"]
    assert _running_run_id_completer(session, "zzz") == []


def test_running_run_id_completer_empty_session_returns_empty() -> None:
    """Tier 2: session with no running skills returns empty list."""
    from reyn.slash.chat import _running_run_id_completer

    session = _StubSession(run_ids=[])
    assert _running_run_id_completer(session, "abc") == []


def test_running_run_id_completer_handles_no_attribute() -> None:
    """Tier 2: session without ``running_skills`` returns empty (defensive)."""
    from reyn.slash.chat import _running_run_id_completer

    class _Bare:
        pass

    assert _running_run_id_completer(_Bare(), "") == []


def test_intervention_id_completer_returns_active_ids() -> None:
    """Tier 2: empty arg_partial returns all active intervention IDs."""
    from reyn.slash.chat import _intervention_id_completer

    session = _StubSession(interventions=[
        _StubIntervention("iv-aaa"),
        _StubIntervention("iv-bbb"),
    ])
    out = _intervention_id_completer(session, "")
    assert set(out) == {"iv-aaa", "iv-bbb"}


def test_intervention_id_completer_filters_by_prefix() -> None:
    """Tier 2: prefix filter narrows."""
    from reyn.slash.chat import _intervention_id_completer

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
    from reyn.slash.chat import _intervention_id_completer

    session = _StubSession(interventions=[_StubIntervention("iv-aaa")])
    assert _intervention_id_completer(session, "iv-aaa hello") == []
    assert _intervention_id_completer(session, "iv- text") == []


def test_intervention_id_completer_no_registry_returns_empty() -> None:
    """Tier 2: session without ``_interventions`` returns empty (defensive)."""
    from reyn.slash.chat import _intervention_id_completer

    class _Bare:
        pass

    assert _intervention_id_completer(_Bare(), "") == []


def test_cancel_slash_has_completer_registered() -> None:
    """Tier 2: ``/cancel`` registers ``_running_run_id_completer``."""
    from reyn.slash import REGISTRY
    from reyn.slash.chat import _running_run_id_completer

    cmd = REGISTRY.get("cancel")
    assert cmd is not None
    assert cmd.completer is _running_run_id_completer


def test_answer_slash_has_completer_registered() -> None:
    """Tier 2: ``/answer`` registers ``_intervention_id_completer``."""
    from reyn.slash import REGISTRY
    from reyn.slash.chat import _intervention_id_completer

    cmd = REGISTRY.get("answer")
    assert cmd is not None
    assert cmd.completer is _intervention_id_completer
