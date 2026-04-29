"""App-name → filesystem-path resolution.

Used by both the CLI (run/eval/lint) and the runtime (run_app Control IR op)
to find an app's directory under reyn/local, reyn/project, or stdlib.

Lives outside the CLI package so the runtime doesn't depend on CLI internals.
"""
from __future__ import annotations
import sys
from pathlib import Path


def stdlib_root() -> Path:
    """Absolute path to the bundled stdlib/ tree."""
    return Path(__file__).parent.parent / "stdlib"


def resolve_app_path(name: str) -> tuple[Path, Path]:
    """Resolve a short app name to (app_dir, dsl_root).

    Search order: reyn/local → reyn/project → stdlib.
    Exits with status 1 if not found.
    """
    sl = stdlib_root()
    candidates: list[tuple[Path, Path]] = [
        (Path("reyn") / "local" / name,   Path("reyn")),
        (Path("reyn") / "project" / name, Path("reyn")),
        (sl / "apps" / name,              sl),
    ]
    for app_dir, dsl_root in candidates:
        if (app_dir / "app.md").exists():
            return app_dir, dsl_root
    checked = "\n  ".join(str(d / "app.md") for d, _ in candidates)
    print(f"Error: app '{name}' not found. Looked in:\n  {checked}", file=sys.stderr)
    sys.exit(1)
