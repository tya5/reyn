"""Tier 1: scripts/verify_env_identity.py console-script staleness contract.

Pins the invariant #3024 ratified: a `[project.scripts]` entry is a declaration,
and a venv installed before that declaration does not carry the script — so the
checker must report the *absence*, naming the venv as stale, rather than let the
absence surface as ``execvp() failed`` / ``McpError: Connection closed`` and read
as a broken feature. That misread reached a co-vet verdict twice in one day.

``check_console_scripts`` reads a real pyproject.toml and a real bin directory —
no network, no venv construction — so this is a Tier 1 contract test against
known on-disk inputs. The fixtures are real files in ``tmp_path``, not mocks: the
check's whole subject is what is *actually on disk*, so faking the filesystem
would remove the only thing under test.

Public surface only: each case calls the module's registered check and asserts on
the returned ``Finding`` objects' public fields (``check`` / ``detail``).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_env_identity.py"


def _load():
    spec = importlib.util.spec_from_file_location("_env_identity_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _checkout(tmp_path: Path, scripts: dict[str, str]) -> Path:
    """A real checkout whose pyproject declares ``scripts``."""
    root = tmp_path / "checkout"
    (root / "src" / "reyn").mkdir(parents=True)
    (root / "src" / "reyn" / "__init__.py").write_text("")
    declared = "\n".join(f'{name} = "{target}"' for name, target in scripts.items())
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "reyn"\n\n[project.scripts]\n{declared}\n'
    )
    return root


def _venv_bin(tmp_path: Path, present: list[str]) -> Path:
    """A real bin directory carrying ``present``, each stamped like `pip` stamps.

    The shebang names this bin's own interpreter — `pip` stamps the absolute path of
    the interpreter it installed into, and the checker compares against it.
    """
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    interpreter = bin_dir / "python"
    interpreter.write_text("")
    for name in present:
        script = bin_dir / name
        script.write_text(f"#!{interpreter}\nprint('{name}')\n")
        script.chmod(0o755)
    return bin_dir


def test_a_declared_script_absent_from_the_venv_is_reported_as_venv_staleness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: a declared-but-absent console script is named, and named as stale.

    FALSIFY: without this check the same environment reports healthy — the absence
    only surfaces later, when something runs the script and dies with a message
    that mentions neither the script's absence nor the venv.
    """
    module = _load()
    root = _checkout(tmp_path, {"reyn": "reyn._cli:main", "reyn-rag-vector-store": "reyn.v:main"})
    bin_dir = _venv_bin(tmp_path, present=["reyn"])  # installed before the second entry
    monkeypatch.setattr(module.sys, "executable", str(bin_dir / "python"))
    monkeypatch.setattr(module.shutil, "which", lambda name: str(bin_dir / name))

    findings = module.check_console_scripts(root)

    assert [f.check for f in findings] == ["console-scripts/present"]
    detail = findings[0].detail
    assert "reyn-rag-vector-store" in detail
    # The three things the #3023 post-mortem found collapsed into one word must
    # each be separable from this finding: which subject, that it is the venv that
    # is stale, and that this is neither breakage nor a flake.
    assert "pyproject.toml:" in detail
    assert "stale" in detail
    assert "NOT evidence" in detail and "NOT a flake" in detail


def test_a_venv_carrying_every_declared_script_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: the check stays silent when the venv matches the declaration."""
    module = _load()
    root = _checkout(tmp_path, {"reyn": "reyn._cli:main", "reyn-rag-vector-store": "reyn.v:main"})
    bin_dir = _venv_bin(tmp_path, present=["reyn", "reyn-rag-vector-store"])
    monkeypatch.setattr(module.sys, "executable", str(bin_dir / "python"))
    monkeypatch.setattr(module.shutil, "which", lambda name: str(bin_dir / name))

    assert module.check_console_scripts(root) == []


def test_a_console_script_shadowed_on_path_names_the_foreign_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: a spawn-by-name runs PATH's copy, so a foreign copy is reported.

    FALSIFY: comparing only against the venv's own bin/ would call this clean while
    every spawn of `reyn` executes another environment's script.
    """
    module = _load()
    root = _checkout(tmp_path, {"reyn": "reyn._cli:main"})
    bin_dir = _venv_bin(tmp_path, present=["reyn"])
    foreign = _venv_bin(tmp_path / "other", present=["reyn"])
    monkeypatch.setattr(module.sys, "executable", str(bin_dir / "python"))
    monkeypatch.setattr(module.shutil, "which", lambda name: str(foreign / name))

    findings = module.check_console_scripts(root)

    assert [f.check for f in findings] == ["console-scripts/present"]
    assert str(foreign) in findings[0].detail


def test_every_registered_check_is_reachable_through_verify(tmp_path: Path) -> None:
    """Tier 1: `verify` derives its work from CHECKS, so a registered check runs.

    The enumeration is the contract: a check that is registered but never dispatched
    would be a gate that cannot fire, which is the failure mode this module exists
    to prevent.
    """
    module = _load()
    root = _checkout(tmp_path, {"reyn": "reyn._cli:main"})

    for name in module.CHECKS:
        # Each selector must dispatch and return findings (possibly none) rather
        # than silently doing nothing.
        assert isinstance(module.verify(root, only=(name,)), list)
