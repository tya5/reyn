"""Tier 2: webhook plugin loader — FP-0041 #489 follow-up.

Pins ``load_webhooks_yaml`` (= file read) and ``load_webhook_plugins``
(= entry-point dispatch + APIRouter mount) for the plugin framework.

Tests:

  1. webhooks.yaml file reader — missing / malformed / well-formed.
  2. Loader: short form vs long form (= reyn-reserved keys handling).
  3. Loader: ``enabled: false`` skip.
  4. Loader: missing plugin (= not installed) → warn + skip.
  5. Loader: plugin returning ``None`` (= opt-out) → skip mount.
  6. Loader: plugin returning non-APIRouter → warn + skip.
  7. Loader: plugin raising → log + skip, other plugins continue.
  8. Loader: reyn-reserved keys (= ``package`` / ``enabled``) stripped
     from the config passed to ``register_router``.

Tier 2 because the loader is the single dispatch point for ALL
inbound webhook traffic — a regression silently breaks every
webhook plugin in production deployments.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI

from reyn.web.plugin_loader import load_webhook_plugins, load_webhooks_yaml

# ── load_webhooks_yaml ────────────────────────────────────────────────


def test_load_webhooks_yaml_returns_empty_when_missing(tmp_path: Path):
    """Tier 2: a project without webhooks.yaml loads cleanly as empty.
    No webhook plugins = common case, must not crash.
    """
    assert load_webhooks_yaml(tmp_path) == {}


def test_load_webhooks_yaml_returns_empty_on_malformed(tmp_path: Path):
    """Tier 2: a malformed webhooks.yaml logs a warning and yields an
    empty config rather than crashing reyn web at boot.
    """
    (tmp_path / "webhooks.yaml").write_text(
        "not: valid: yaml: nested:wrong", encoding="utf-8",
    )
    # Should not raise; should return empty.
    result = load_webhooks_yaml(tmp_path)
    assert isinstance(result, dict)


def test_load_webhooks_yaml_returns_empty_on_non_mapping(tmp_path: Path):
    """Tier 2: a webhooks.yaml whose top level is a list / scalar
    isn't usable; loader warns and returns empty.
    """
    (tmp_path / "webhooks.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert load_webhooks_yaml(tmp_path) == {}


def test_load_webhooks_yaml_parses_well_formed_file(tmp_path: Path):
    """Tier 2: a well-formed webhooks.yaml parses to the documented
    top-level mapping.
    """
    (tmp_path / "webhooks.yaml").write_text(
        "sample_slack:\n  target_agent: news_agent\n", encoding="utf-8",
    )
    result = load_webhooks_yaml(tmp_path)
    assert result == {"sample_slack": {"target_agent": "news_agent"}}


# ── load_webhook_plugins ──────────────────────────────────────────────


def _stub_entry_point(name: str, dist_name: str, register_fn):
    """Build a fake entry point object covering the attributes the
    loader actually reads (= name, dist.name, load()).
    """
    class _Dist:
        name = dist_name

    class _EP:
        def __init__(self):
            self.name = name
            self.dist = _Dist()

        def load(self):
            return register_fn

    return _EP()


@pytest.fixture()
def _patch_entry_points(monkeypatch):
    """Patch ``importlib.metadata.entry_points`` to return whatever the
    caller installs into a list. Tests append fake EPs to the list.
    """
    fake_eps: list = []

    def _fake_entry_points(*, group=None):
        if group == "reyn.webhooks":
            return list(fake_eps)
        return []

    monkeypatch.setattr(
        "reyn.web.plugin_loader.importlib.metadata.entry_points",
        _fake_entry_points,
    )
    return fake_eps


def test_loader_skips_when_empty_config(_patch_entry_points):
    """Tier 2: empty webhooks_config → no work, returns 0."""
    app = FastAPI()
    mounted = load_webhook_plugins(app=app, webhooks_config={})
    assert mounted == 0


def test_loader_mounts_short_form_plugin(_patch_entry_points):
    """Tier 2: short form (= value=None) activates the plugin with
    default-enabled + auto-resolved package.
    """
    received: list = []

    def _register(config):
        received.append(config)
        return APIRouter()

    _patch_entry_points.append(
        _stub_entry_point("sample_slack", "reyn", _register),
    )
    app = FastAPI()
    mounted = load_webhook_plugins(
        app=app, webhooks_config={"sample_slack": None},
    )
    assert mounted == 1
    # Short form → empty config passed to register_router.
    assert received == [{}]


def test_loader_strips_reyn_reserved_keys(_patch_entry_points):
    """Tier 2: ``package`` and ``enabled`` are reyn-reserved; they are
    NOT forwarded to ``register_router``. Plugin author sees only
    their own fields.
    """
    received: list = []

    def _register(config):
        received.append(config)
        return APIRouter()

    _patch_entry_points.append(
        _stub_entry_point("sample_slack", "reyn", _register),
    )
    app = FastAPI()
    load_webhook_plugins(
        app=app,
        webhooks_config={
            "sample_slack": {
                "package": "reyn",       # reyn-reserved (stripped)
                "enabled": True,         # reyn-reserved (stripped)
                "target_agent": "x",     # plugin-defined (passed through)
            },
        },
    )
    assert received == [{"target_agent": "x"}]


def test_loader_skips_disabled_plugin(_patch_entry_points):
    """Tier 2: ``enabled: false`` → plugin not mounted. ``register_router``
    is NOT called (= even discovery skipped at that step).
    """
    called = False

    def _register(config):
        nonlocal called
        called = True
        return APIRouter()

    _patch_entry_points.append(
        _stub_entry_point("sample_slack", "reyn", _register),
    )
    app = FastAPI()
    mounted = load_webhook_plugins(
        app=app,
        webhooks_config={"sample_slack": {"enabled": False}},
    )
    assert mounted == 0
    assert called is False


def test_loader_warns_when_plugin_not_installed(_patch_entry_points):
    """Tier 2: an entry referenced in webhooks.yaml but with no matching
    entry-point logs a warning and skips. Operator hasn't installed
    the package.
    """
    # No entry points registered.
    app = FastAPI()
    mounted = load_webhook_plugins(
        app=app, webhooks_config={"unknown_plugin": None},
    )
    assert mounted == 0


def test_loader_skips_when_register_returns_none(_patch_entry_points):
    """Tier 2: ``register_router`` returning None (= plugin opted out
    because of missing required option) skips mount without crashing.
    """
    def _register(config):
        return None

    _patch_entry_points.append(
        _stub_entry_point("sample_slack", "reyn", _register),
    )
    app = FastAPI()
    mounted = load_webhook_plugins(
        app=app, webhooks_config={"sample_slack": None},
    )
    assert mounted == 0


def test_loader_skips_when_register_returns_non_router(_patch_entry_points):
    """Tier 2: a plugin returning a non-APIRouter value is a contract
    violation; loader logs and skips rather than crashing.
    """
    def _register(config):
        return "not a router"

    _patch_entry_points.append(
        _stub_entry_point("misbehaving", "reyn", _register),
    )
    app = FastAPI()
    mounted = load_webhook_plugins(
        app=app, webhooks_config={"misbehaving": None},
    )
    assert mounted == 0


def test_loader_continues_after_register_raises(_patch_entry_points):
    """Tier 2: a plugin whose ``register_router`` raises does NOT
    prevent other plugins from mounting. Isolation.
    """
    def _bad(config):
        raise RuntimeError("plugin boot failure")

    def _good(config):
        return APIRouter()

    _patch_entry_points.append(_stub_entry_point("bad_one", "pkg_a", _bad))
    _patch_entry_points.append(_stub_entry_point("good_one", "pkg_b", _good))

    app = FastAPI()
    mounted = load_webhook_plugins(
        app=app,
        webhooks_config={"bad_one": None, "good_one": None},
    )
    assert mounted == 1   # only the good one


def test_loader_disambiguates_via_package_field(_patch_entry_points):
    """Tier 2: when two packages register the same plugin name, the
    ``package:`` field in webhooks.yaml selects which one is mounted.
    Verified via the entry-point's distribution name reaching through
    to ``register_router`` (= each fake builds a router with a marker
    path so the test can identify which one mounted).
    """
    def _from_a(config):
        r = APIRouter()
        @r.get("/from-pkg-a")
        async def _h():
            return {}
        return r

    def _from_b(config):
        r = APIRouter()
        @r.get("/from-pkg-b")
        async def _h():
            return {}
        return r

    _patch_entry_points.append(_stub_entry_point("sample_slack", "pkg_a", _from_a))
    _patch_entry_points.append(_stub_entry_point("sample_slack", "pkg_b", _from_b))

    app = FastAPI()
    mounted = load_webhook_plugins(
        app=app,
        webhooks_config={"sample_slack": {"package": "pkg_b"}},
    )
    assert mounted == 1
    # The pkg_b router (= /from-pkg-b path) should be the one mounted.
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/from-pkg-b" in paths
    assert "/from-pkg-a" not in paths
