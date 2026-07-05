"""reyn.hooks.matcher — interpret ``HookDef.matcher`` to filter hook firing (#2608 H2).

H1 added the first EXTERNAL-event hook-point, ``mcp_resource_updated``, but a
hook registered on it fires for ANY subscribed-resource update — scoping is
only via which resources the user chose to subscribe to. H2 lets a hook
narrow further: ``matcher`` is a small ``field -> pattern`` dict evaluated
against the event's ``template_vars`` at dispatch time, BEFORE the hook's
action runs.

Shape
-----
``matcher: dict[str, str] | None`` — e.g. ``{"server": "github", "uri":
"file:///repo/**"}`` or (H4) ``{"path": "/repo/src/**"}``. A hook fires iff
**every** field named in the matcher matches:

- exact string equality for every field EXCEPT ``uri``/``path``;
- ``uri``/``path`` match via a shell-style glob (``fnmatch.fnmatch``), so
  ``"file:///repo/**"`` matches any URI under that prefix and
  ``"/repo/src/**"`` matches any watched path under that directory.

A field named in the matcher that is ABSENT from ``template_vars`` (e.g. a
lifecycle hook-point's vars have no ``server``/``uri``, or a future source's
vars don't carry a field this hook's matcher names) never matches — a
matcher can only narrow, never invent a signal that was never fired.

Absent or empty matcher -> the hook always fires (unchanged behavior for
every hook that predates H2, and for an external-event hook that opts not to
filter). This is the byte-identical-to-H1 default: ``matches(None, ...)`` and
``matches({}, ...)`` are both ``True`` unconditionally.

Deliberately general: the glob-vs-exact rule is keyed off the FIELD NAME, not
the hook-point, so a future external-event source (cron/webhook, H5) that
also emits a ``uri``- or ``path``-shaped field gets glob matching for free,
and any other field it introduces gets exact matching with zero code changes
here.

#2608 H4 adds ``path`` to ``_GLOB_FIELDS`` for the ``file_changed`` point
(see ``reyn.runtime.fs_watcher``): a hook's matcher can scope to a watched
sub-tree, e.g. ``{"path": "/repo/src/**"}``.
"""
from __future__ import annotations

from fnmatch import fnmatch

# Field names matched via glob (fnmatch) rather than exact string equality.
# "uri" — #2608 H1 (mcp_resource_updated). "path" — #2608 H4 (file_changed):
# lets a hook's matcher scope to a sub-tree via e.g. {"path": "/repo/src/**"}.
_GLOB_FIELDS: frozenset[str] = frozenset({"uri", "path"})


def matches(matcher: "dict[str, str] | None", template_vars: dict) -> bool:
    """Return whether a hook whose ``HookDef.matcher`` is *matcher* should fire
    for an event whose dispatch context is *template_vars*.

    ``matcher`` is ``None``/empty -> always ``True`` (fire-always default,
    preserving pre-H2 behavior for every existing hook — lifecycle hooks never
    set a matcher, and an H1-era external-event hook with no matcher keeps
    firing on every subscribed-resource update).
    """
    if not matcher:
        return True
    for field_name, pattern in matcher.items():
        value = template_vars.get(field_name)
        if value is None:
            return False  # the field this matcher names was never fired with a value
        value_str = str(value)
        if field_name in _GLOB_FIELDS:
            if not fnmatch(value_str, pattern):
                return False
        elif value_str != pattern:
            return False
    return True


__all__ = ["matches"]
