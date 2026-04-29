"""
App resolution and loading shared by `run` and `eval` subcommands.

`resolve_app_path` — name → directory under reyn/local, reyn/project, stdlib.
`load_app_from_args` — handles all three CLI ways of pointing at an app
                       (positional name, --app-path, --module).
"""
from __future__ import annotations
import argparse
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

from reyn.app_paths import resolve_app_path, stdlib_root  # re-exported for convenience
from reyn.models import App

__all__ = ["LoadedApp", "load_app_from_args", "resolve_app_path", "stdlib_root"]


@dataclass
class LoadedApp:
    app: App
    app_md: Path | None       # None when source == "module"
    dsl_root: str | None
    source: str               # "name" | "path" | "module"


def load_app_from_args(args: argparse.Namespace) -> LoadedApp:
    """Resolve `args.app_name | args.app_path | args.module` and load the App."""
    if getattr(args, "app_path", None):
        app_dir = Path(args.app_path)
        app_md = app_dir / "app.md"
        dsl_root = args.dsl_root
        return LoadedApp(
            app=_compile(app_md, dsl_root),
            app_md=app_md, dsl_root=dsl_root, source="path",
        )

    if getattr(args, "app_name", None):
        app_dir, inferred_root = resolve_app_path(args.app_name)
        dsl_root = args.dsl_root or str(inferred_root)
        app_md = app_dir / "app.md"
        print(f"resolved        : {app_md}  (dsl-root: {dsl_root})")
        return LoadedApp(
            app=_compile(app_md, dsl_root),
            app_md=app_md, dsl_root=dsl_root, source="name",
        )

    if getattr(args, "module", None):
        try:
            module = importlib.import_module(args.module)
        except ModuleNotFoundError as e:
            print(f"Error: cannot import module '{args.module}': {e}", file=sys.stderr)
            sys.exit(1)
        if not hasattr(module, "app"):
            print(f"Error: module '{args.module}' has no 'app' attribute.", file=sys.stderr)
            sys.exit(1)
        return LoadedApp(app=module.app, app_md=None, dsl_root=None, source="module")

    print("Error: provide an app name (positional), --app-path DIR, or --module.",
          file=sys.stderr)
    sys.exit(1)


def _compile(app_md: Path, dsl_root: str | None) -> App:
    from reyn.compiler import load_dsl_app
    try:
        return load_dsl_app(str(app_md), dsl_root=dsl_root)
    except Exception as e:
        print(f"Error: failed to compile DSL '{app_md}': {e}", file=sys.stderr)
        sys.exit(1)
