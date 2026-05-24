"""Agent profile listing for Chainlit's chat-profile picker.

Pure helper (= no chainlit import) so unit tests run without the
``[chainlit]`` extra. The decorator-side caller in ``app`` consumes the
returned dicts and wraps each with ``cl.ChatProfile(**d)``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class _RegistryLike(Protocol):
    """Minimal surface this module needs from ``AgentRegistry``.

    Defined as a Protocol so the unit tests can pass a tiny fake
    without dragging in the real registry's constructor dependencies
    (StateLog / config / etc.).
    """

    def list_names(self) -> list[str]: ...

    def load_profile(self, name: str) -> "_ProfileLike": ...


class _ProfileLike(Protocol):
    name: str
    role: str


@dataclass(frozen=True)
class ChatProfileDict:
    """Plain-data shape consumed by ``cl.ChatProfile(**fields)``.

    Mirrors ``chainlit.ChatProfile``'s public fields. Keeping the dict
    here (instead of importing cl.ChatProfile) lets tests run without
    the [chainlit] extra.
    """
    name: str
    markdown_description: str
    icon: str | None = None

    def as_kwargs(self) -> dict:
        out = {"name": self.name, "markdown_description": self.markdown_description}
        if self.icon is not None:
            out["icon"] = self.icon
        return out


_NO_ROLE_MARKER = "_(no role description)_"


def list_agent_profiles(registry: _RegistryLike) -> list[ChatProfileDict]:
    """Return one ChatProfileDict per agent on disk.

    Picks each agent's ``role`` as the markdown description; falls back
    to a dim italic marker when role is empty. Agents are sorted by
    name (= same order as ``registry.list_names()``), so the picker is
    stable across reloads.
    """
    out: list[ChatProfileDict] = []
    for name in registry.list_names():
        profile = registry.load_profile(name)
        role = (getattr(profile, "role", "") or "").strip()
        description = role if role else _NO_ROLE_MARKER
        out.append(ChatProfileDict(name=name, markdown_description=description))
    return out


__all__ = ["ChatProfileDict", "list_agent_profiles"]
