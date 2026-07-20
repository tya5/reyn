"""Tier 2: OS invariant -- every shipped SKILL.md fits under the default
(model-unresolved) ``read_file`` inline cap (#3162 part 1).

**Motivation (the hole this closes).** ``read_file``'s inline cap for an
unbounded body read is WINDOW-DERIVED (``_read_inline_cap`` in
``src/reyn/core/op_runtime/file.py`` -> ``control_ir_inline_cap`` in
``src/reyn/core/context_builder.py``): it grows with the resolved model's
context window, but when no model resolves (``ctx.resolver`` absent, or
resolution raises) it FALLS BACK to the fixed floor,
``MAX_CONTROL_IR_RESULT_INLINE_BYTES`` (currently 8_192 chars). A skill's
``SKILL.md`` body is read via the ordinary ``file`` read op (A3 in
``reyn.builtin.registry``'s module docstring: "the model finds it by
calling ``skill_list``, then reads its ``SKILL.md`` body with the ordinary
``file`` read op"). A ``SKILL.md`` larger than the default floor is
therefore silently truncated -- and WHICH skills truncate depends on which
model is resolved at read time, the worst kind of failure (the same
content behaves differently depending on an orthogonal runtime variable).

Before #3162, ``build_and_query_rag_corpus/SKILL.md`` shipped at 21_837
bytes -- 266% of the 8_192-char default floor, already truncated in
practice for any caller without a large-window model resolved. #3162 part 1
split it into three skills that each fit; THIS gate is what keeps any
future skill (builtin or plugin) from silently regressing past the floor
again.

**Enumeration -- real directory walk + registry, no hardcoded name list**
(so a newly added skill, in either surface, is picked up automatically):

1. ``BUILTIN_SKILLS`` (``src/reyn/builtin/registry.py``) -- the always-on
   builtin skills.
2. Every ``SKILL.md`` under ``src/reyn/builtin/plugins/*/skills/*/`` -- the
   plugin-shipped skills (the RAG plugin's, at time of writing). These are
   OUT OF SCOPE for ``tests/test_builtin_registry_disk_parity.py`` (#3168),
   which explicitly excludes ``builtin/plugins/**`` from its own disk walk
   because plugin skills register through a structurally different,
   install-time path (``reyn.core.op_runtime.plugin_install``), not
   ``BUILTIN_SKILLS``. That is the correct scope for #3168's registration-
   reachability gate, but it means the 266%-of-cap file that motivated THIS
   gate would be invisible to a gate that only walked ``BUILTIN_SKILLS`` --
   so this module deliberately unions both surfaces instead of reusing
   #3168's narrower enumeration.

**Cap value** is imported directly from
``reyn.core.context_builder.MAX_CONTROL_IR_RESULT_INLINE_BYTES`` -- the same
constant ``_read_inline_cap``'s no-model fallback returns -- rather than a
magic number re-declared in this file, so a future change to the floor
cannot silently desync from what this gate checks.

No fakes: reads the REAL constant and the REAL files on disk.
"""
from __future__ import annotations

from pathlib import Path

import reyn.builtin.registry as registry_module
from reyn.builtin.registry import BUILTIN_SKILLS
from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES

_BUILTIN_DIR = Path(registry_module.__file__).parent
_PLUGINS_DIR = _BUILTIN_DIR / "plugins"


def _builtin_registry_skill_paths() -> "set[Path]":
    """Every ``BUILTIN_SKILLS`` entry's SKILL.md (the always-on surface)."""
    return {Path(entry["path"]).resolve() for entry in BUILTIN_SKILLS.values()}


def _plugin_skill_md_paths_on_disk() -> "set[Path]":
    """Every ``SKILL.md`` shipped under any builtin plugin's ``skills/``
    directory -- a real directory walk, NOT a hardcoded plugin/skill name
    list, so a newly added plugin skill (or plugin) is picked up
    automatically. This is the surface #3168's registry<->disk parity gate
    (``tests/test_builtin_registry_disk_parity.py``) explicitly excludes,
    because plugin skills register through the install-time
    ``plugin_install`` path rather than ``BUILTIN_SKILLS`` -- but it is
    exactly the surface that shipped an over-cap file (#3162)."""
    if not _PLUGINS_DIR.is_dir():
        return set()
    return {p.resolve() for p in _PLUGINS_DIR.glob("*/skills/*/SKILL.md")}


def _all_shipped_skill_md_paths() -> "set[Path]":
    return _builtin_registry_skill_paths() | _plugin_skill_md_paths_on_disk()


def test_every_shipped_skill_md_fits_the_default_inline_cap() -> None:
    """Tier 2: OS invariant -- every SKILL.md reachable via either shipping
    surface (BUILTIN_SKILLS registry entries + plugin skills-on-disk) must
    be strictly smaller than the model-unresolved default inline cap
    (``MAX_CONTROL_IR_RESULT_INLINE_BYTES``), so a `file` read of the body
    never silently truncates regardless of which model (if any) is
    resolved at read time (see module docstring)."""
    all_paths = _all_shipped_skill_md_paths()
    assert len(all_paths) >= 1, (
        "vacuity guard: no SKILL.md found across either shipping surface -- "
        "this gate would pass trivially with nothing to check"
    )
    # Sub-vacuity guards: a silent regression in either enumerator (e.g. the
    # plugin glob pattern drifting from the real on-disk layout) must fail
    # LOUDLY here rather than quietly shrinking the checked set to zero for
    # that surface while the combined-count guard above still passes.
    registry_paths = _builtin_registry_skill_paths()
    plugin_paths = _plugin_skill_md_paths_on_disk()
    assert len(registry_paths) >= 1, (
        "vacuity guard: BUILTIN_SKILLS enumerated zero paths"
    )
    assert len(plugin_paths) >= 1, (
        "vacuity guard: the builtin/plugins/*/skills/*/SKILL.md glob found "
        "nothing -- either no plugin ships a skill (unexpected -- the rag "
        "plugin does) or the glob pattern drifted from the real layout"
    )

    oversize = {}
    for path in sorted(all_paths):
        size = len(path.read_text(encoding="utf-8"))
        if size >= MAX_CONTROL_IR_RESULT_INLINE_BYTES:
            oversize[str(path)] = size
    assert not oversize, (
        "SKILL.md exceeds the default (model-unresolved) inline read cap "
        f"({MAX_CONTROL_IR_RESULT_INLINE_BYTES} chars) -- a `file` read of "
        "its body silently truncates whenever no model (or a small-window "
        f"model) resolves at read time: {oversize}"
    )
