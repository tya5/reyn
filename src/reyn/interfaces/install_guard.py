"""Diagnosable install-guard messaging for optional-extra dependencies.

Several entry points (``reyn web``, ``reyn chat --connect``) require optional
dependency groups declared in ``pyproject.toml`` extras (e.g. ``[web]``). When
the import of such a dependency fails, the guard must tell the operator *why* in
a way that actually resolves the failure — two diagnosability traps motivate this
module:

1. A **bare** ``pip install -e ".[web]"`` recommendation is fragile: on setups
   where ``pip`` resolves to a different interpreter than the active venv (pyenv
   shims, global site-packages), the install lands in the wrong environment and
   the guard keeps reporting "not installed". The robust form is
   ``python -m pip install -e '.[web]'`` — ``python -m pip`` targets the *running*
   interpreter, and single-quoting the extra stops zsh from globbing ``[web]``.
2. Reporting **every** ``ImportError`` as "not installed" masks version-conflict
   failures (the dependency IS installed, but an incompatible resolved version
   breaks its import). :func:`missing_dep_message` distinguishes a genuinely
   missing module (``ModuleNotFoundError`` whose ``name`` is the package) from an
   installed-but-broken import, and surfaces the real exception text in both.

This module is import-light (stdlib only) on purpose: it must be importable even
when the optional extra it describes is absent.
"""
from __future__ import annotations


def install_command(extra: str) -> str:
    """Return the robust, interpreter-targeted install command for ``extra``.

    ``python -m pip`` installs into the *active* interpreter (not whatever a bare
    ``pip`` shim resolves to); the single quotes protect the ``[extra]`` token
    from shell globbing (zsh).
    """
    return f"python -m pip install -e '.[{extra}]'"


def missing_dep_message(exc: ImportError, package: str, extra: str) -> str:
    """Build a diagnosable guard message for a failed optional-dependency import.

    Distinguishes two failure modes and phrases each differently:

    * genuinely missing (``ModuleNotFoundError`` whose ``name`` is ``package``)
      → an "is not installed" message with the robust install command.
    * installed-but-broken (any other ``ImportError``, e.g. a version conflict)
      → an "is installed but failed to import" message.

    The underlying exception text is included in *both* messages so the real
    cause is visible instead of being masked as "not installed".
    """
    cmd = install_command(extra)
    note = (
        f"(`{cmd}` installs into the active interpreter; the quotes protect the "
        "extra from shell globbing.)"
    )
    if isinstance(exc, ModuleNotFoundError) and exc.name == package:
        return (
            f"Error: {package} is not installed ({exc}). "
            f"Run `{cmd}` to install the [{extra}] dependencies. {note}"
        )
    return (
        f"Error: {package} is installed but failed to import: {exc} — "
        f"likely a version conflict (the [{extra}] extra pins compatible versions, "
        f"e.g. fastapi/starlette; check the installed versions). "
        f"If it is genuinely missing, run `{cmd}`. {note}"
    )
