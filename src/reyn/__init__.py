"""reyn — Agent OS public API (lazy-loaded top-level names).

The package's public names are resolved lazily via PEP 562 ``__getattr__`` so
that importing a *submodule* — notably ``reyn.core.kernel._python_harness``, the
python preprocessor-step child entry point — does NOT eagerly pull the
agent / llm / httpx chain in through this ``__init__``.

This extends the FP-0008 C4 lazy-litellm fix (which made ``import litellm``
lazy) one layer down: the harness path now imports only what it needs
(allowlist + stdlib), so its cold import stays well under the python-step
timeout. The eager chain (``SkillRuntime`` -> ``llm`` -> ``httpx``) cost ~0.5s,
which under the in-container venv path on an emulated host inflated past the ~5s
step timeout and aborted steps. ``from reyn import SkillRuntime`` / ``reyn.SkillRuntime``
still work — they trigger the lazy load on first access.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import-cost-free hints for type checkers / IDEs
    from reyn.core.kernel.runtime import RunResult
    from reyn.schemas.models import Phase, Skill, SkillGraph
    from reyn.skill_runtime import SkillRuntime

__all__ = ["Skill", "Phase", "SkillGraph", "SkillRuntime", "RunResult"]

_LAZY_ATTRS = {
    "SkillRuntime": "reyn.skill_runtime",
    "RunResult": "reyn.core.kernel.runtime",
    "Phase": "reyn.schemas.models",
    "Skill": "reyn.schemas.models",
    "SkillGraph": "reyn.schemas.models",
}


def __getattr__(name: str):
    import importlib

    module_path = _LAZY_ATTRS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_path), name)


def __dir__():
    return sorted(__all__)
