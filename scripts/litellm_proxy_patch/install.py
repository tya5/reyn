#!/usr/bin/env python3
"""reyn #5620 — install/uninstall ``litellm_proxy_patch.py`` into a
target Python environment's ``site-packages``.

Deliberately standalone (stdlib only — no ``reyn`` import, matching
``litellm_proxy_patch.py``'s own "runtime only" constraint, owner ruling
2026-08-30). Run with the TARGET environment's own interpreter (e.g. the
proxy venv's ``python``), not reyn's — this script resolves
``site-packages`` off ``sys.path``/``sysconfig`` for whichever Python
invokes it, by design (installing FOR a different interpreter than the
one running this script is not a shape this script supports; run it
with the target venv activated, or via that venv's own
``python install.py``).

## Why a ``.pth`` file at all (not "tell the operator to add an import")

A ``.pth`` file in ``site-packages`` is read by Python's own
``site`` module at interpreter startup, before any application code
runs — the SAME mechanism the pre-#5620 hand-placed patch already used
(and the SAME failure mode this design inherits knowledge of: a broken
``.pth`` line degrades to a startup warning, "Remainder of file
ignored", and the process keeps running — silently unpatched). This
installer's own ``.pth`` line is a single, minimal
``import litellm_proxy_patch`` (architect's own #5620 ruling) — the
narrowest possible line, so the ONLY way it can go stale is the module
itself failing to import, which ``litellm_proxy_patch.py``'s own
``apply()``/``_write_status()`` already degrade safely from (never
raises into the interpreter startup). The status-file absence (or
staleness) is therefore the detection signal for "the patch did not
apply this run" — this installer never tries to make the ``.pth``
itself self-diagnosing beyond that.
"""
from __future__ import annotations

import argparse
import shutil
import site
import sys
from pathlib import Path

_PATCH_FILENAME = "litellm_proxy_patch.py"
_PTH_FILENAME = "zz_reyn_litellm_proxy_patch.pth"
_PTH_CONTENT = "import litellm_proxy_patch\n"


def _resolve_site_packages() -> Path:
    """Return the CURRENT interpreter's own site-packages — the one
    ``site`` itself will scan for ``.pth`` files at every future startup
    of this SAME interpreter. ``site.getsitepackages()`` covers a venv;
    ``site.ENABLE_USER_SITE`` / ``site.getusersitepackages()`` is the
    fallback for a user-scheme install (no venv) where
    ``getsitepackages()`` can raise or return an unwritable system
    path."""
    try:
        candidates = site.getsitepackages()
    except Exception:  # noqa: BLE001 — some interpreter builds omit this call entirely
        candidates = []
    for c in candidates:
        p = Path(c)
        if p.is_dir():
            return p
    user_site = site.getusersitepackages()
    return Path(user_site)


def _install(site_packages: Path) -> None:
    site_packages.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve().parent / _PATCH_FILENAME
    if not src.is_file():
        raise FileNotFoundError(f"{_PATCH_FILENAME} not found next to install.py: {src}")
    dst = site_packages / _PATCH_FILENAME
    shutil.copyfile(src, dst)
    pth = site_packages / _PTH_FILENAME
    pth.write_text(_PTH_CONTENT, encoding="utf-8")
    print(f"installed: {dst}")
    print(f"installed: {pth} ({_PTH_CONTENT.strip()!r})")
    print(
        "status file (written the next time litellm is imported in this "
        "environment): ~/.reyn/litellm-proxy-patch-status.json"
    )


def _uninstall(site_packages: Path) -> None:
    removed = []
    for name in (_PATCH_FILENAME, _PTH_FILENAME):
        p = site_packages / name
        if p.exists():
            p.unlink()
            removed.append(str(p))
    # A pre-#5620 hand-placed install may still carry the OLD .pth name —
    # remove it too so `--uninstall` genuinely leaves nothing behind,
    # not just this installer's own new filename.
    legacy_pth = site_packages / "zz_litellm_patch.pth"
    legacy_patch = site_packages / "litellm_patch.py"
    for p in (legacy_pth, legacy_patch):
        if p.exists():
            p.unlink()
            removed.append(str(p))
    if removed:
        print("removed:")
        for r in removed:
            print(f"  {r}")
    else:
        print(f"nothing to remove in {site_packages}")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--uninstall", action="store_true",
        help="remove the patch file and .pth line (this and any pre-#5620 legacy install) instead of installing",
    )
    parser.add_argument(
        "--site-packages", default=None,
        help="override the auto-resolved site-packages directory (advanced; mostly for tests)",
    )
    args = parser.parse_args(argv)

    site_packages = (
        Path(args.site_packages).expanduser().resolve()
        if args.site_packages else _resolve_site_packages()
    )
    print(f"target site-packages: {site_packages}")

    if args.uninstall:
        _uninstall(site_packages)
    else:
        _install(site_packages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
