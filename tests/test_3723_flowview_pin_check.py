"""Tier 1: scripts/verify_env_identity.py's textual-flowview pin contract.

Pins the invariant #3723 ratified: 4 of 4 sessions measured a full suite
against a mis-pinned `textual-flowview` on the same day — three had a stale
version and read real test failures as "pre-existing on origin/main"; the
fourth had the pinned commit's VERSION but was reading a local working copy,
which a version-only check cannot distinguish from the real pin. The
constructed invariant: `check_flowview_pin` reads `pip`'s own recorded
provenance (`direct_url.json`) and the `find_spec` origin, and reports a
Finding whenever either signals the installed package did not come from the
pinned commit — never by importing `textual_flowview` itself.

No mocks: each case builds a real `pyproject.toml` on disk and monkeypatches
only the process-identity seams the check reads through (`sys.prefix`,
`importlib.util.find_spec`, `importlib.metadata.distribution`) — the same
idiom `test_3024_env_identity.py` already uses for `sys.executable` /
`shutil.which`.

Public surface only: each case calls the module's registered check and
asserts on the returned `Finding` objects' public fields (`check` / `detail`).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_env_identity.py"

_PIN_URL = "https://github.com/tya5/textual-flowview.git"
_PIN_SHA = "5a72da086f1e8e3fc0c93b0806b53a5fd5fb1c7f"


def _load():
    spec = importlib.util.spec_from_file_location("_env_identity_under_test_3723", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _checkout(tmp_path: Path) -> Path:
    """A real checkout whose pyproject pins textual-flowview like the real one."""
    root = tmp_path / "checkout"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "reyn"\n'
        "dependencies = [\n"
        f'    "textual-flowview @ git+{_PIN_URL}@{_PIN_SHA}",\n'
        '    "numpy>=1.24",\n'
        "]\n"
    )
    return root


class _FakeDistribution:
    def __init__(self, version: str, direct_url: dict | None) -> None:
        self.version = version
        self._direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        if filename == "direct_url.json" and self._direct_url is not None:
            return json.dumps(self._direct_url)
        return None


def _wire(
    module,
    monkeypatch: pytest.MonkeyPatch,
    *,
    origin: str | None,
    prefix: str,
    dist: _FakeDistribution | None,
) -> None:
    fake_spec = None if origin is None else type("S", (), {"origin": origin})()
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: fake_spec)
    monkeypatch.setattr(module.sys, "prefix", prefix)

    def _distribution(name: str):
        if dist is None:
            raise module.importlib.metadata.PackageNotFoundError(name)
        return dist

    monkeypatch.setattr(module.importlib.metadata, "distribution", _distribution)


def test_the_pinned_commit_installed_in_site_packages_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: an install matching the pin's url+commit, resolved under
    sys.prefix, reports no finding — the real, healthy shape."""
    module = _load()
    root = _checkout(tmp_path)
    prefix = str(tmp_path / "venv")
    origin = f"{prefix}/lib/python3.12/site-packages/textual_flowview/__init__.py"
    dist = _FakeDistribution(
        "0.12.0",
        {"url": _PIN_URL, "vcs_info": {"commit_id": _PIN_SHA}},
    )
    _wire(module, monkeypatch, origin=origin, prefix=prefix, dist=dist)

    assert module.check_flowview_pin(root) == []


def test_a_stale_commit_is_named_with_both_shas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: a version-mismatched install is reported as `flowview-pin/stale`
    naming both the pin and what is actually installed.

    FALSIFY: without this check, a stale venv reports a test failure with no
    hint that the venv (not the tree) is the cause — the #3723 incident.
    """
    module = _load()
    root = _checkout(tmp_path)
    prefix = str(tmp_path / "venv")
    origin = f"{prefix}/lib/python3.12/site-packages/textual_flowview/__init__.py"
    stale_sha = "0" * 40
    dist = _FakeDistribution(
        "0.9.0",
        {"url": _PIN_URL, "vcs_info": {"commit_id": stale_sha}},
    )
    _wire(module, monkeypatch, origin=origin, prefix=prefix, dist=dist)

    findings = module.check_flowview_pin(root)

    assert [f.check for f in findings] == ["flowview-pin/stale"]
    detail = findings[0].detail
    assert _PIN_SHA in detail
    assert stale_sha in detail


def test_a_local_working_copy_is_flagged_even_when_version_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: an install with the pinned VERSION but no VCS commit record and
    an origin outside site-packages is `flowview-pin/local-copy`, not clean.

    FALSIFY: the tui-coder shape from #3723 — version-only comparison would
    call this clean. A local clone can drift the moment its own directory
    changes, silently, with no re-install to catch it.
    """
    module = _load()
    root = _checkout(tmp_path)
    prefix = str(tmp_path / "venv")
    local_clone = str(tmp_path / "some" / "other" / "checkout" / "textual_flowview" / "__init__.py")
    dist = _FakeDistribution(
        "0.12.0",
        {"url": f"file://{tmp_path}/some/other/checkout", "dir_info": {"editable": True}},
    )
    _wire(module, monkeypatch, origin=local_clone, prefix=prefix, dist=dist)

    findings = module.check_flowview_pin(root)

    assert [f.check for f in findings] == ["flowview-pin/local-copy"]
    assert "0.12.0" in findings[0].detail


def test_an_uninstalled_flowview_is_flagged_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: no `find_spec` origin at all reports `flowview-pin/absent`."""
    module = _load()
    root = _checkout(tmp_path)
    _wire(module, monkeypatch, origin=None, prefix=str(tmp_path / "venv"), dist=None)

    findings = module.check_flowview_pin(root)

    assert [f.check for f in findings] == ["flowview-pin/absent"]
    assert _PIN_SHA in findings[0].detail or _PIN_SHA in findings[0].remedy


def test_flowview_pin_is_reachable_through_verify(tmp_path: Path) -> None:
    """Tier 1: `flowview-pin` is registered and dispatched, not orphaned."""
    module = _load()

    assert "flowview-pin" in module.CHECKS
    root = _checkout(tmp_path)
    # No monkeypatching: exercises the real installed environment this suite
    # itself runs under — the same environment #3723 needs verified.
    assert isinstance(module.verify(root, only=("flowview-pin",)), list)


# ── partition_flowview_findings (#3725 review: local-copy needs a real opt-out) ─


def _local_copy_finding(module) -> object:
    return module.Finding(check="flowview-pin/local-copy", detail="d", remedy="r")


def _stale_finding(module) -> object:
    return module.Finding(check="flowview-pin/stale", detail="d", remedy="r")


def test_local_copy_blocks_without_a_reason(tmp_path: Path) -> None:
    """Tier 1: an unset/empty REYN_FLOWVIEW_LOCAL_COPY still blocks — silence
    is not an opt-out (lead-coder review: the opt-out must require a reason,
    mirroring `repo_root_cwd(reason=...)`)."""
    module = _load()
    finding = _local_copy_finding(module)

    for reason in ("", "   "):
        blocking, acknowledged = module.partition_flowview_findings([finding], reason)
        assert blocking == [finding]
        assert acknowledged == []


def test_local_copy_is_acknowledged_with_a_real_reason(tmp_path: Path) -> None:
    """Tier 1: a non-empty reason downgrades local-copy from blocking to
    acknowledged — the #3725 fix for the tui-coder case (legitimate local
    clone, no way to say so before this)."""
    module = _load()
    finding = _local_copy_finding(module)

    blocking, acknowledged = module.partition_flowview_findings(
        [finding], "developing textual-flowview itself, PR #123"
    )

    assert blocking == []
    assert acknowledged == [finding]


def test_stale_never_gets_acknowledged_even_with_a_reason(tmp_path: Path) -> None:
    """Tier 1: `flowview-pin/stale` has no opt-out at all — a version
    mismatch is never a legitimate local-development shape the way a clone
    is (lead-coder review of #3725: "stale側は abort のままで正しい")."""
    module = _load()
    finding = _stale_finding(module)

    blocking, acknowledged = module.partition_flowview_findings(
        [finding], "some reason"
    )

    assert blocking == [finding]
    assert acknowledged == []
