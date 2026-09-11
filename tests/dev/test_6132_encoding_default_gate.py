"""Tier 2: #6132's own gate — a reyn.* text-mode file open without
``encoding=`` must fail CI, on ANY host locale, not only a non-utf-8 one.

## Why this is platform-independent (the strip-falsify CLAUDE.md requires)

The #6132 bug ("cp932 で reyn.log が書けず...") only manifests as a crash
on a non-utf-8-locale host, because that is the one case where the OMITTED
``encoding=`` actually fails to encode a real character. A test that writes
an em dash and reads it back would pass on this repo's own utf-8 CI even
with the bug PRESENT — vacuous, exactly what the issue warns against.

:class:`EncodingWarning` (PEP 597) sidesteps this: it fires the moment
``encoding=`` is omitted, regardless of what locale the process is running
under or whether the write would actually succeed there. Reverting any of
#6132's ``encoding="utf-8"`` additions in ``src/`` makes the corresponding
scenario below fail on THIS machine's own locale (almost certainly utf-8),
not only on a Windows/cp932 host — the platform-independence the issue
requires.

## Real subprocesses, not in-process

Two different claims need two different rigs:

- The first three scenarios below exercise the raw mechanism (a bare
  ``warnings.filterwarnings`` + ``PYTHONWARNDEFAULTENCODING``, in an
  isolated ``sys.executable -c`` script) — proving the flag itself is
  load-bearing needs toggling it OFF for one scenario, something only a
  fresh interpreter can do. ``out_of_process_reyn`` pins each spawn's
  ``PYTHONPATH`` to the SAME checkout this test file itself imports
  (#5028).
- The fourth, ``test_a_real_pytest_run_rejects_the_same_omission``, is the
  one that actually matters operationally: a REAL ``sys.executable -m
  pytest`` run, using THIS repo's own ``pyproject.toml``/rootdir, is what
  CI actually runs. A bare ``warnings.filterwarnings()`` call at
  ``tests/conftest.py`` IMPORT time was tried first and found NOT to
  protect this path — pytest enters a fresh ``catch_warnings()`` +
  ``simplefilter("always")`` around every test item's own execution,
  discarding any filter installed at collection/import time before the
  item's body runs (reproduced directly: "1 passed, 1 warning", no
  error). ``[tool.pytest.ini_options].filterwarnings`` in
  ``pyproject.toml`` is pytest's OWN mechanism for this — re-applied
  fresh per item — and is what this scenario actually exercises.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def _run(
    src_root: str, script: str, *, warn_default_encoding: bool,
) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": src_root}
    if warn_default_encoding:
        env["PYTHONWARNDEFAULTENCODING"] = "1"
    else:
        env.pop("PYTHONWARNDEFAULTENCODING", None)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True, text=True, env=env,
    )


# The SAME filter pyproject.toml's [tool.pytest.ini_options].filterwarnings
# entry expresses for the real suite (kept in sync by hand -- both copies
# are documented at each end: pyproject.toml's own comment names this
# file, and this docstring below points back). One line, so interpolating
# it into an indented triple-quoted script below never fights
# textwrap.dedent over inconsistent per-line indentation.
_INSTALL_FILTER = (
    'import warnings; warnings.filterwarnings('
    '"error", category=EncodingWarning, module=r"reyn(\\..*)?$")'
)


def test_a_reyn_open_without_encoding_raises_when_the_gate_is_armed(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2: the positive case — proves the filter+flag combination
    actually CATCHES a real #6132-shaped omission. ``encoding_gate_probe``
    (``reyn.dev.testing``) exists ONLY to give this test a genuine
    ``reyn.*``-namespaced call site (the filter is scoped by the warning's
    attributed module, not by this test file's own — see pyproject.toml's
    own `filterwarnings` comment for why a call from this file would not
    be caught)."""
    script = f"""
        {_INSTALL_FILTER}
        from reyn.dev.testing.encoding_gate_probe import open_without_encoding_for_gate_test
        open_without_encoding_for_gate_test({str(tmp_path / "probe.txt")!r})
        print("UNREACHABLE — the EncodingWarning above should have raised")
        """
    result = _run(out_of_process_reyn, script, warn_default_encoding=True)
    assert result.returncode != 0, (
        f"the gate did not catch an unencoded reyn.* open; stdout={result.stdout!r}"
    )
    assert "EncodingWarning" in result.stderr, result.stderr


def test_the_same_open_is_silent_without_the_flag(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2: deny side — proves the flag itself is load-bearing, not just
    the filter. Without ``PYTHONWARNDEFAULTENCODING=1``, CPython never
    produces an ``EncodingWarning`` at all (the flag is latched at
    interpreter startup, per pyproject.toml's own comment) — the SAME
    unencoded open above must complete silently here. If this scenario
    ever raised, the positive test above would be meaningless (every
    process would fail regardless of the flag)."""
    script = f"""
        {_INSTALL_FILTER}
        from reyn.dev.testing.encoding_gate_probe import open_without_encoding_for_gate_test
        open_without_encoding_for_gate_test({str(tmp_path / "probe2.txt")!r})
        print("OK — no EncodingWarning without the flag")
        """
    result = _run(out_of_process_reyn, script, warn_default_encoding=False)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_the_6132_fix_itself_is_clean_under_the_armed_gate(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2: the #6132 fix's own strip-falsify target. ``_setup_interactive_
    logging`` (``interfaces/cli/commands/chat.py``) is the exact call path
    #6132's owner-hit reproduction traced the bug to — it must run cleanly
    under the SAME armed gate the two scenarios above use.

    Strip-falsify (verified by hand, file-internal Edit only, reverted):
    removing chat.py's ``encoding="utf-8"`` kwarg from the
    ``FailureFallbackRotatingFileHandler`` construction makes this test fail
    — on THIS machine's own (utf-8) locale, not only a cp932 one — because
    the gate is a static-explicitness check, not a real encode failure."""
    script = f"""
        {_INSTALL_FILTER}
        from pathlib import Path
        from reyn.interfaces.cli.commands.chat import _setup_interactive_logging

        _setup_interactive_logging(Path({str(tmp_path)!r}))
        print("OK — the real #6132 call path raised no EncodingWarning")
        """
    result = _run(out_of_process_reyn, script, warn_default_encoding=True)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


_PROBE_TEST_SOURCE = (
    "def test_probe():\n"
    "    from reyn.dev.testing.encoding_gate_probe import "
    "open_without_encoding_for_gate_test\n"
    "    import tempfile, os\n"
    "    open_without_encoding_for_gate_test("
    "os.path.join(tempfile.mkdtemp(), 'p.txt'))\n"
)


def test_a_real_pytest_run_rejects_the_same_omission(
    out_of_process_reyn: str,
) -> None:
    """Tier 2: the scenario that actually matters — a REAL ``sys.executable
    -m pytest`` run, invoked from this repo's own root (so its
    ``pyproject.toml`` — ``[tool.pytest.ini_options].filterwarnings`` — is
    the one in effect, not a throwaway pytester tree), must FAIL a real
    reyn.* omission, not merely warn about it.

    This is the regression #6132's own BLOCKING review caught: the first
    version of this gate (a bare ``warnings.filterwarnings()`` call at
    ``tests/conftest.py`` import time) passed all three scenarios above
    while doing NOTHING for the real suite — pytest's own per-item
    ``catch_warnings()`` reset discarded it before any test body ran. This
    scenario is what would have caught that: it spawns the actual command
    CI runs, not a hand-rolled reproduction of the mechanism.

    A throwaway single-test file is written under ``tests/dev/`` (not
    ``tmp_path``, which pytest's rootdir discovery would not resolve back
    to this repo's own ``pyproject.toml``) and removed in a ``finally``,
    so nothing this test creates is ever left behind to commit."""
    probe_path = Path(__file__).parent / "_tmp_6132_probe_for_gate_test.py"
    probe_path.write_text(_PROBE_TEST_SOURCE, encoding="utf-8")
    try:
        env = {**os.environ, "PYTHONPATH": out_of_process_reyn, "PYTHONWARNDEFAULTENCODING": "1"}
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(probe_path), "-q"],
            capture_output=True, text=True, env=env,
            cwd=str(Path(out_of_process_reyn).parent),
        )
    finally:
        probe_path.unlink(missing_ok=True)

    assert result.returncode != 0, (
        f"a real pytest run did not reject an unencoded reyn.* open; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "1 failed" in result.stdout, result.stdout
    assert "EncodingWarning" in result.stdout, result.stdout
