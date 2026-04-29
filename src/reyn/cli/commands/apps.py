"""`reyn apps` — list available apps or show details for one."""
from __future__ import annotations
import argparse
from pathlib import Path

from ..app_loader import stdlib_root


def register(sub) -> None:
    p = sub.add_parser("apps", help="List available apps, or show usage details for one app")
    p.add_argument("app_name", nargs="?", default=None, metavar="APP",
                   help="App name to show details for (omit to list all)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    sl = stdlib_root()
    search_roots: list[tuple[str, Path]] = [
        ("project", Path("reyn") / "project"),
        ("local",   Path("reyn") / "local"),
        ("stdlib",  sl / "apps"),
    ]

    if args.app_name:
        _print_detail(args.app_name, search_roots)
        return

    _print_list(search_roots)


def _read_app_md(app_md: Path) -> tuple[dict, str]:
    from reyn.compiler.parser import _split_frontmatter
    try:
        fm, body = _split_frontmatter(app_md.read_text(encoding="utf-8"))
        return fm, body
    except Exception:
        return {}, ""


def _find_app(name: str, search_roots: list[tuple[str, Path]]) -> Path | None:
    for _, apps_dir in search_roots:
        candidate = apps_dir / name / "app.md"
        if candidate.exists():
            return candidate
    return None


def _print_detail(name: str, search_roots: list[tuple[str, Path]]) -> None:
    app_md = _find_app(name, search_roots)
    if app_md is None:
        print(f"App '{name}' not found.")
        return
    fm, body = _read_app_md(app_md)
    print(f"\n{fm.get('name', name)}")
    if fm.get("description"):
        print(f"{fm['description']}\n")
    if body.strip():
        print(body.strip())
    else:
        print("(no documentation)")
    print()


def _print_list(search_roots: list[tuple[str, Path]]) -> None:
    found_any = False
    seen: set[str] = set()
    for label, apps_dir in search_roots:
        if not apps_dir.exists():
            continue
        entries = sorted(p for p in apps_dir.iterdir()
                         if p.is_dir() and (p / "app.md").exists())
        if not entries:
            continue
        print(f"\n{label}  ({apps_dir})")
        for app_dir in entries:
            name = app_dir.name
            fm, _ = _read_app_md(app_dir / "app.md")
            desc = (fm.get("description") or "").strip().splitlines()[0] if fm.get("description") else ""
            shadowed = " [shadowed]" if name in seen else ""
            desc_str = f"  — {desc}" if desc else ""
            print(f"  {name}{desc_str}{shadowed}")
            seen.add(name)
        found_any = True

    if not found_any:
        print("No apps found.")
    print()
    print("Run 'reyn apps <name>' for usage details.")
