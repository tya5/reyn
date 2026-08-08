"""#3869: reyn names its own process, so `ps` can tell it from any other python.

The interesting assertion here is not that a function returns a string — it is
that the *operating system* changed what it reports for a live process. That
needs a real child process, because the title is a property of the process, not
of the module: `set_process_title` returning True is a producer-side claim, and
today's #3850 (`WrappedCommand.env` required, populated, tested, and read by
nobody) is what a producer-side claim alone is worth.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from reyn.runtime.proctitle import format_title, set_process_title


def test_title_is_the_subcommand_and_nothing_else():
    """Tier 1: #3869 — the name reyn shows is `reyn:<subcommand>`.

    Pinned separately from the OS behaviour because the naming rule is a
    contract with the operator (they read this string), while the OS call is a
    mechanism. Strip `PREFIX` or the separator and this goes RED without
    needing a process.
    """
    assert format_title("chat") == "reyn:chat"
    assert format_title("serve") == "reyn:serve"
    # No subcommand is a real case: `reyn` with no args, and anything that
    # calls the helper before parsing. Naming it "reyn:" would be worse than
    # naming it "reyn".
    assert format_title(None) == "reyn"
    assert format_title("") == "reyn"


def test_ps_reports_the_new_name_for_a_live_process():
    """Tier 2: #3869 — the OS actually reports the title for a running process.

    The child sets its title and then reads its OWN `ps` line, so there is
    nothing to wait for: no sleep, no poll, no timeout. The parent asserts on
    what the OS said about a process that was alive at the moment it said it.

    Replace `set_process_title` with a no-op (or drop `setproctitle` from the
    dependencies) and this goes RED with the interpreter's own name, which is
    exactly the state #3869 exists to remove.
    """
    child = textwrap.dedent(
        """
        import os, subprocess, sys
        sys.path.insert(0, {src!r})
        from reyn.runtime.proctitle import set_process_title
        set_process_title("test-probe")
        print(subprocess.run(
            ["ps", "-p", str(os.getpid()), "-o", "args="],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        """
    ).format(src=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

    out = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True, check=True
    ).stdout.strip()

    assert out == "reyn:test-probe", (
        f"ps reported {out!r}; the process is still identifying itself by "
        "interpreter, which is the failure #3869 is about"
    )


def test_setting_the_title_reports_whether_it_took_effect():
    """Tier 1: #3869 — the caller can tell a real rename from a silent no-op.

    `set_process_title` returns False when `setproctitle` is absent rather than
    raising, and the return value is the only way a caller distinguishes
    "renamed" from "still python3.12". A helper that always returned None would
    make those two indistinguishable — the same shape as a gate whose failure
    looks like its success.
    """
    assert set_process_title("selftest") is True
