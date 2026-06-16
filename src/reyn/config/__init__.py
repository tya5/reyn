"""reyn.config — configuration package (#1682 #3: god-module split).

The former 2.8k-line ``config.py`` is split into a ``config/`` package grouped by
domain concern. This ``__init__`` re-exports the FULL surface of every submodule
(public names AND the de-facto-public underscore privates — ``_find_project_root``
alone has 23 importers, plus many ``_build_*`` and module constants like
``_DEFAULT_EMBEDDING_CLASSES``), so the ~135 ``from reyn.config import X`` call
sites are unchanged — zero call-site rewrites.

The re-export is done **mechanically** (copy every non-dunder name out of each
submodule) rather than a hand-maintained list, so a name can never be silently
omitted (the R4 hazard). The R4 guard test
(test_config_package_reexport_1682) pins that the de-facto-public set resolves here.

Submodules (#1682 grouping, by domain concern not yaml-key shape):
    root       — ReynConfig + model/tier fields + model_class_for
    loader     — load_config / _merge / _load_yaml / shape-wiring (yaml-coupled core)
    chat       — Reasoning/Chat/Loop/Compaction/Timeout/OnLimit/Safety
    embedding  — Embedding/SkillSearch/ActionRetrieval
    media      — Voice/Multimodal/Web/WebFetch
    execution  — Plan/SkillResume/SelfImprovement/TimeTravel/ToolUse
    infra      — Agent/Auth/Sandbox/Events/Eval/Cron/Python
"""
# Commit 1 (#1682 #3): all definitions still live in `root`; the submodule split
# follows. Re-export every name from each section module so the package surface ==
# the union of the sections. Listed explicitly (not `import *`) so the dependency
# is auditable; the per-module namespace copy below also picks up underscore
# privates + constants that `import *` would skip.
from reyn.config import root as _root

_SECTIONS = (_root,)


def _reexport() -> None:
    g = globals()
    for _mod in _SECTIONS:
        for _name in vars(_mod):
            if not _name.startswith("__"):
                g[_name] = getattr(_mod, _name)


_reexport()
