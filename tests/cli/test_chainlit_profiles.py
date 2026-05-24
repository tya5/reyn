"""Tier 1: ``reyn.chainlit_app.profiles.list_agent_profiles`` contract.

The chainlit-side ``@cl.set_chat_profiles`` decorator consumes the
list this helper returns and wraps each entry with
``cl.ChatProfile(**dict.as_kwargs())``. Three invariants pinned:

1. Each on-disk agent surfaces as exactly one ChatProfileDict.
2. Empty-role agents land a fallback markdown marker (= the picker
   should never show a blank description cell).
3. Order is whatever ``registry.list_names()`` returns (= the
   registry's stable sort), so the picker order is reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass

from reyn.chainlit_app.profiles import (
    _NO_ROLE_MARKER,
    ChatProfileDict,
    list_agent_profiles,
)


@dataclass
class _FakeProfile:
    name: str
    role: str


class _FakeRegistry:
    """Smallest possible thing that satisfies ``list_agent_profiles``."""

    def __init__(self, profiles: list[_FakeProfile]) -> None:
        self._profiles = {p.name: p for p in profiles}
        # ``registry.list_names()`` returns names sorted on disk; mirror that.
        self._order = sorted(self._profiles)

    def list_names(self) -> list[str]:
        return list(self._order)

    def load_profile(self, name: str) -> _FakeProfile:
        return self._profiles[name]


def test_returns_one_dict_per_agent():
    """Tier 1: 3 on-disk agents → 3 ChatProfileDict entries, in registry order."""
    reg = _FakeRegistry([
        _FakeProfile(name="default", role="general assistant"),
        _FakeProfile(name="alpha", role="research"),
        _FakeProfile(name="beta", role="code review"),
    ])
    out = list_agent_profiles(reg)
    assert [d.name for d in out] == ["alpha", "beta", "default"]
    assert [d.markdown_description for d in out] == [
        "research", "code review", "general assistant",
    ]


def test_empty_role_uses_fallback_marker():
    """Tier 1: empty / whitespace-only role → italic placeholder so the
    picker cell is never blank."""
    reg = _FakeRegistry([
        _FakeProfile(name="bare", role=""),
        _FakeProfile(name="space", role="   "),
        _FakeProfile(name="real", role="has a role"),
    ])
    out = {d.name: d.markdown_description for d in list_agent_profiles(reg)}
    assert out["bare"] == _NO_ROLE_MARKER
    assert out["space"] == _NO_ROLE_MARKER
    assert out["real"] == "has a role"


def test_as_kwargs_omits_icon_when_none():
    """Tier 1: ``ChatProfileDict.as_kwargs`` matches ``cl.ChatProfile``'s
    accepted kwargs; ``icon`` omitted unless set so chainlit picks its
    default avatar."""
    d = ChatProfileDict(name="x", markdown_description="y")
    assert d.as_kwargs() == {"name": "x", "markdown_description": "y"}
    d2 = ChatProfileDict(name="x", markdown_description="y", icon="/foo.png")
    assert d2.as_kwargs() == {
        "name": "x", "markdown_description": "y", "icon": "/foo.png",
    }


def test_role_attr_missing_treated_as_empty():
    """Tier 1: a profile-like object without ``role`` attribute (= future
    AgentProfile shape change) still works — fallback to marker, no crash."""

    @dataclass
    class _RolelessProfile:
        name: str

    class _Reg:
        def list_names(self) -> list[str]:
            return ["x"]
        def load_profile(self, name: str) -> _RolelessProfile:
            return _RolelessProfile(name=name)

    out = list_agent_profiles(_Reg())
    assert len(out) == 1
    assert out[0].markdown_description == _NO_ROLE_MARKER
