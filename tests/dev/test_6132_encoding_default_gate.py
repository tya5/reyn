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

``tests/conftest.py``'s filter (and the ``PYTHONWARNDEFAULTENCODING=1`` env
CI's ``test.yml`` sets) already covers the real suite — but proving that
combination CATCHES something needs a controlled, isolated process: the
flag is latched at interpreter startup (cannot be armed from inside a
running test), and this file needs to toggle it OFF for one scenario (to
prove the flag itself is load-bearing, not merely the filter) — something
only a fresh subprocess can do. ``out_of_process_reyn`` pins each spawn's
``PYTHONPATH`` to the SAME checkout this test file itself imports (#5028).
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


# The SAME filter tests/conftest.py installs (kept in sync by hand — the two
# copies are documented at both ends: conftest.py's own comment names this
# file, and this docstring below points back). One line, so interpolating it
# into an indented triple-quoted script below never fights textwrap.dedent
# over inconsistent per-line indentation.
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
    attributed module, not by this test file's own — see conftest.py's own
    comment for why a call from this file would not be caught)."""
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
    interpreter startup, per conftest.py's own comment) — the SAME
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
