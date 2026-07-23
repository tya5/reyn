"""Production-reachable read path for REGISTERED-plugin BODY content.

Mirrors ``reyn.builtin.docs.read_builtin_body_bytes`` (#2913/#2914) for
``~/.reyn/plugins/<name>/`` content, closing a builtin/plugin asymmetry: a
builtin skill/pipeline's shipped body (``reyn.builtin.registry``'s
``BUILTIN_SKILLS``/``BUILTIN_PIPELINES`` entries) already short-circuits the
generic ``read_file`` op's ``_in_default_read_zone`` gate
(``reyn.security.permissions.permissions``) because it resolves outside
``project_root`` in every deploy. A plugin's ``skills/**`` / ``pipelines/**``
content resolves outside ``project_root`` too (``~/.reyn/plugins/`` is a
per-OPERATOR global cache, ADR 0064 §3.3), but had NO equivalent
short-circuit — it fell through to the same out-of-root gate, which
hard-denies non-interactively (no operator to approve). A skill's own
frontmatter can legitimately point an LLM at a bundled reference file
(``references/*.md`` under its own ``skills/<name>/`` dir) that then turns
out to be unreachable without an interactive approval that never comes in a
non-interactive run — "the reference is documented but unreachable."

**Trust boundary: install-registration, not marker presence.** A naive
"resolves under a directory carrying a ``.reyn-plugin/`` marker" check would
let anyone create ``~/.reyn/plugins/evil/.reyn-plugin/`` +
``skills/x/SKILL.md`` by hand and read it bypass-free — the marker alone
proves nothing about operator consent. The bypass here is instead keyed off
:func:`reyn.core.op_runtime.plugin_install.is_registered_plugin_root`: TRUE
only for a plugin root that reached ``plugin_install``'s step 9 completion
(source-resolve → manifest-validate → operator-permission-gated copy →
capability-register all succeeded).

**This is NOT the same strength as the builtin case, and that gap is
deliberate, not overlooked.** ``read_builtin_body_bytes``'s guarantee comes
from the PACKAGE ITSELF — ``importlib.resources`` only ever resolves to
content the wheel ships, so nothing reachable at runtime can plant a fake
entry there. This module's guarantee instead comes from a FILE on disk (the
completion sidecar, ``.reyn-plugin/_source_kind.json``) — and anyone who can
write under ``~/.reyn/plugins/`` at all (the SAME write capability
``plugin_install``'s own gate 1 requires) can plant a forged sidecar
alongside a hand-placed ``SKILL.md``, exactly as they could forge the marker
itself. So this is weaker than the builtin boundary, by construction — the
reason it is still acceptable: an attacker with that write capability
already has direct write access to ``~/.reyn/plugins/<name>/skills/**``
itself, i.e. they could just edit an ALREADY-registered plugin's ``SKILL.md``
body directly and reach the exact same LLM-visible outcome without forging
anything. Forging the sidecar grants no capability beyond what that same
attacker already had — this bypass does not widen the attack surface, but it
should not be described as builtin-equivalent, and no future change should
assume it is.

**Scope (least-privilege, mirrors ``_BODY_READ_DIRS``).** Only
``skills/**`` and ``pipelines/**`` under a REGISTERED plugin root bypass the
gate. Everything else remains normally gated, in particular:

- ``scripts/`` — plugin-declared, potentially-EXECUTED code (same reason
  ``read_builtin_body_bytes`` excludes ``.py`` modules outside its body
  dirs: this bypass authorizes reading shipped *documentation/config*
  content, not arbitrary plugin-authored code).
- ``requirements.txt``, ``.mcp.json`` — not body content.
- ``.staging/`` — ``plugin_install``'s git-clone staging area
  (``plugin_install.py``'s ``handle``, ``{kind: "git"}`` branch) holds
  content BEFORE the operator's install-permission gate has run (or before a
  ``git`` install's run-code trust gate has even asked); it is explicitly
  excluded even though nothing under ``.staging/`` is ever a completed,
  registered plugin root anyway (an in-flight clone has no
  ``.reyn-plugin/_source_kind.json`` sidecar) — the exclusion is a defense
  in depth against ANY future ambiguity in how ``.staging/`` names are
  formed, not merely a load-bearing check.

**Deliberately NOT keyed off enable/disable.** ``skills.yaml``/
``pipelines.yaml`` enablement is a project-local "use it or don't" toggle
over content already approved once, globally, at install time — see
:func:`~reyn.core.op_runtime.plugin_install.is_registered_plugin_root`'s
docstring for the full rationale. Coupling this bypass to enablement would
also make "many projects enable one shared global copy" interact with a
per-project on/off switch in a way that complicates the read-time check for
no security benefit (simplicity is itself a security property here).
"""
from __future__ import annotations

from pathlib import Path

# The ONLY subdirectories of a registered plugin root whose files are
# legitimate L2/L3 body reads — mirrors
# ``reyn.builtin.docs._BODY_READ_DIRS`` exactly (same two capability kinds,
# same rationale: a path resolving inside a registered plugin root but
# outside these body dirs returns ``None`` and falls through to the normal
# read-zone gate).
_BODY_READ_DIRS = frozenset({"skills", "pipelines"})

# Never a registered plugin root — ``plugin_install``'s git-clone staging
# area (see module docstring). Checked before the registry lookup so a
# ``.staging/`` path never even reaches ``is_registered_plugin_root``.
_STAGING_DIR_NAME = ".staging"


def read_plugin_body_bytes(path_str: str) -> "bytes | None":
    """Read of a REGISTERED plugin's skill/pipeline BODY file.

    Returns the file's raw bytes when *path_str* resolves to a file INSIDE
    one of a REGISTERED ``~/.reyn/plugins/<name>/`` root's BODY directories
    (``skills/`` or ``pipelines/`` — see ``_BODY_READ_DIRS``). *path_str* MAY
    also be a skill's DIRECTORY (``skills.entries.<name>.path`` is registered
    as the directory, not the file — see ``plugin_install.py``), in which
    case ``<dir>/SKILL.md`` is read instead (mirrors
    ``skill_install._resolve_skill_md``'s identical convention). Returns
    ``None`` in every other case — not under ``~/.reyn/plugins/`` at all, an
    UNREGISTERED plugin root (never installed, mid-install, or rolled back),
    ``.staging/``, or inside a registered root but outside its body dirs
    (``scripts/``, ``requirements.txt``, ``.mcp.json``, etc.). In every
    ``None`` case the caller (``reyn.core.op_runtime.file.handle``) falls
    through to the normal ``_in_default_read_zone``-gated file read,
    unchanged — identical fallback contract to
    ``reyn.builtin.docs.read_builtin_body_bytes``.
    """
    # Local import: avoids a module-load-order dependency on
    # ``reyn.core.op_runtime`` (this module is imported early, at
    # ``reyn.core.op_runtime.file`` module-load time) — resolved lazily,
    # at call time, well after both packages have finished importing.
    from reyn.core.op_runtime.plugin_install import (
        is_registered_plugin_root,
        plugins_root,
    )

    try:
        root = plugins_root().resolve()
    except OSError:
        return None

    try:
        candidate = Path(path_str).expanduser().resolve()
    except OSError:
        return None

    try:
        rel = candidate.relative_to(root)
    except ValueError:
        return None  # not under ~/.reyn/plugins — not a plugin body, let the normal gate handle it

    if len(rel.parts) < 2:
        return None  # no plugin-name/body-dir prefix at all

    plugin_name, body_dir, *_rest = rel.parts
    if plugin_name == _STAGING_DIR_NAME:
        return None  # in-flight install content — never approved yet

    # Least-privilege scoping: inside a plugin root but outside a body dir → gated.
    if body_dir not in _BODY_READ_DIRS:
        return None

    plugin_root = root / plugin_name
    if not is_registered_plugin_root(plugin_root):
        return None  # unregistered / mid-install / rolled-back root — never bypasses the gate

    try:
        # `skills.entries.<name>.path` is registered as the SKILL DIRECTORY,
        # not the SKILL.md file (`plugin_install.py` passes
        # ``path=str(skill_dir)`` to ``skill_install``, mirroring how a
        # non-plugin skill install also registers a directory) — `:name`
        # (``reyn.interfaces.skill_invoke.resolve_skill_body``) resolves
        # THAT registered path verbatim, so *candidate* here is routinely a
        # directory, not a file. Append ``SKILL.md`` for that case, mirroring
        # ``skill_install._resolve_skill_md``'s identical dir → `<dir>/
        # SKILL.md` convention exactly (the same resolution the non-plugin
        # skill path already applies) — a plain filesystem read otherwise
        # raises ``IsADirectoryError`` (`[Errno 21] Is a directory`), which
        # silently broke every plugin-installed skill's `:name` invocation
        # (`resolve_skill_body`'s bare ``p.read_text()`` fallback has no
        # directory handling of its own). This runs AFTER the marker/
        # provenance gate above, against the SAME already-verified
        # ``plugin_root`` — appending a literal, non-traversable path
        # component cannot escape it, so the security boundary is
        # unaffected. A FILE candidate (the ordinary file-read skill-load
        # path, and pipeline body entries — always registered as a specific
        # file, never a directory) is untouched, byte-identical to before.
        if candidate.is_dir():
            candidate = candidate / "SKILL.md"
        if not candidate.is_file():
            return None
        return candidate.read_bytes()
    except (OSError, NotADirectoryError):
        return None
