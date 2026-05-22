"""Plugin loader for Reyn web — FP-0041 #489 follow-up.

Discovers webhook plugins via the ``reyn.webhooks`` entry-point group,
matches against the operator's ``webhooks.yaml`` activation list, and
mounts each plugin's router on the FastAPI app.

Reyn core stays Slack/LINE/Discord-free; plugin code lives either in
``src/reyn/plugins/<name>/`` (= sample plugins shipped with reyn) or
in separate pip-installable packages.

## Dedicated config file (= ``webhooks.yaml`` at project root)

Plugin activation + per-plugin options live in a dedicated
``webhooks.yaml`` next to ``reyn.yaml`` — separating them so
``reyn.yaml`` stays focused on core Reyn settings and the plugin
namespace doesn't pollute Reyn-known top-level keys.

  # webhooks.yaml — short form (= label is plugin name)
  sample_slack:
    target_agent: news_agent      # plugin author owns this section

  # Long form: explicit reyn-known meta fields ``package`` / ``enabled``
  tricky_plugin:
    package: foo-pkg              # optional, disambiguates same-name plugins
    enabled: false                # optional, default true
    some_option: value            # plugin-defined fields

The mkdocs convention (= name-only activation with options inline) is
the closest analogue; Reyn extends it with the optional ``package:``
field for unambiguous identification across registries.

## Plugin contract

Each plugin's entry-point target must be a callable::

  def register_router(config: dict) -> APIRouter | None: ...

It receives the plugin's per-instance dict (= the value side of the
``<plugin_name>:`` mapping from ``webhooks.yaml``, minus the
reyn-known ``package`` / ``enabled`` keys). Returns the router to
mount, or ``None`` to skip (= e.g. required option missing).
Mounting failure (= exception during register_router) is caught +
logged; other plugins continue.

## Conflict resolution

When the same plugin name is registered by multiple installed packages
AND the ``webhooks.yaml`` entry doesn't pin a ``package:``, the loader
logs a warning + uses the first match. Operators with conflicting
plugins should set the ``package:`` field explicitly.
"""
from __future__ import annotations

import importlib.metadata
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI

logger = logging.getLogger(__name__)

_WEBHOOK_ENTRY_POINT_GROUP = "reyn.webhooks"

# Reserved keys that the loader interprets; everything else in a
# plugin's per-instance dict is forwarded to ``register_router``
# untouched (= plugin author owns the remaining schema).
_REYN_RESERVED_KEYS = frozenset({"package", "enabled"})


def load_webhooks_yaml(project_root: Path) -> dict:
    """Read ``webhooks.yaml`` from the project root.

    Returns the parsed top-level mapping, or an empty dict when the
    file is absent / malformed. The schema is::

      <plugin_name>:
        package: <optional reyn-reserved>
        enabled: <optional reyn-reserved, default true>
        <plugin-defined fields>

    Defensive: a missing file is normal (= operator has no webhook
    plugins), a malformed file logs a warning and yields an empty
    config rather than crashing reyn web.
    """
    path = project_root / "webhooks.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("webhooks.yaml: failed to parse — %s", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "webhooks.yaml: top-level must be a mapping, got %s",
            type(data).__name__,
        )
        return {}
    return data


def _find_entry_point(
    plugin_name: str, *, package: str | None = None,
) -> Any | None:
    """Locate the entry point matching ``plugin_name`` (and optionally
    ``package``).

    Returns the importlib.metadata EntryPoint or ``None`` when no
    match is found. On multiple matches with ``package=None``, the
    first is returned + a warning logged.
    """
    eps = list(importlib.metadata.entry_points(group=_WEBHOOK_ENTRY_POINT_GROUP))
    matches = [ep for ep in eps if ep.name == plugin_name]
    if package is not None:
        matches = [ep for ep in matches if ep.dist and ep.dist.name == package]
    if not matches:
        return None
    if len(matches) > 1 and package is None:
        names = sorted({ep.dist.name for ep in matches if ep.dist})
        logger.warning(
            "webhook plugin %r registered by multiple packages: %s. "
            "Using first match (%s). Pin 'package:' in reyn.yaml to disambiguate.",
            plugin_name, names, matches[0].dist.name if matches[0].dist else "?",
        )
    return matches[0]


def load_webhook_plugins(*, app: FastAPI, webhooks_config: dict) -> int:
    """Mount each activated webhook plugin's router on ``app``.

    Returns the number of plugins successfully mounted (= for log /
    diagnostics).

    Parameters
    ----------
    app:
        FastAPI app to mount routers on.
    webhooks_config:
        Parsed top-level mapping from ``webhooks.yaml``. Keys are
        plugin names; values are dicts holding optional reyn-reserved
        ``package`` / ``enabled`` plus plugin-defined fields, or
        ``None`` for short-form (= no options, default enabled).
    """
    if not isinstance(webhooks_config, dict) or not webhooks_config:
        return 0

    mounted = 0
    for plugin_name, ref in webhooks_config.items():
        if not isinstance(plugin_name, str) or not plugin_name:
            continue
        if ref is None:
            ref_dict: dict = {}
        elif isinstance(ref, dict):
            ref_dict = ref
        else:
            logger.warning(
                "webhooks.yaml: %s — expected mapping or null, got %s; skipping",
                plugin_name, type(ref).__name__,
            )
            continue

        if not ref_dict.get("enabled", True):
            logger.info("webhook plugin %r disabled via config; skipping", plugin_name)
            continue

        package = ref_dict.get("package")
        if package is not None and not isinstance(package, str):
            logger.warning(
                "webhooks.yaml: %s.package — expected string, got %s; ignoring",
                plugin_name, type(package).__name__,
            )
            package = None

        ep = _find_entry_point(plugin_name, package=package)
        if ep is None:
            logger.warning(
                "webhook plugin %r not installed (package=%r); skipping",
                plugin_name, package,
            )
            continue

        try:
            register_fn = ep.load()
        except Exception as exc:
            logger.exception(
                "webhook plugin %r failed to load entry point: %s",
                plugin_name, exc,
            )
            continue

        # Plugin options = the per-instance dict minus reyn-reserved
        # keys. plugin author sees only their own fields.
        plugin_options = {
            k: v for k, v in ref_dict.items() if k not in _REYN_RESERVED_KEYS
        }

        try:
            router = register_fn(plugin_options)
        except Exception as exc:
            logger.exception(
                "webhook plugin %r register_router raised: %s",
                plugin_name, exc,
            )
            continue

        if router is None:
            # Plugin opted out (= missing required option, etc.).
            # ``register_router`` is expected to log the reason.
            continue
        if not isinstance(router, APIRouter):
            logger.warning(
                "webhook plugin %r register_router returned %s, expected APIRouter; "
                "skipping",
                plugin_name, type(router).__name__,
            )
            continue

        app.include_router(router)
        mounted += 1
        logger.info(
            "webhook plugin %r mounted (package=%s)",
            plugin_name, ep.dist.name if ep.dist else "?",
        )

    return mounted
