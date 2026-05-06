"""`reyn skills` — list available skills or show details for one."""
from __future__ import annotations

import argparse
from pathlib import Path

from ..skill_loader import stdlib_root


def register(sub) -> None:
    p = sub.add_parser("skills", help="List available skills, or show usage details for one skill")
    p.add_argument("skill_name", nargs="?", default=None, metavar="SKILL",
                   help="Skill name to show details for (omit to list all)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    sl = stdlib_root()
    search_roots: list[tuple[str, Path]] = [
        ("project", Path("reyn") / "project"),
        ("local",   Path("reyn") / "local"),
        ("stdlib",  sl / "skills"),
    ]

    if args.skill_name:
        _print_detail(args.skill_name, search_roots)
        return

    _print_list(search_roots)


def _read_skill_md(skill_md: Path) -> tuple[dict, str]:
    from reyn.compiler.parser import _split_frontmatter
    try:
        fm, body = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
        return fm, body
    except Exception:
        return {}, ""


def _find_skill(name: str, search_roots: list[tuple[str, Path]]) -> Path | None:
    for _, skills_dir in search_roots:
        candidate = skills_dir / name / "skill.md"
        if candidate.exists():
            return candidate
    return None


def _print_detail(name: str, search_roots: list[tuple[str, Path]]) -> None:
    skill_md = _find_skill(name, search_roots)
    if skill_md is None:
        print(f"Skill '{name}' not found.")
        return
    fm, body = _read_skill_md(skill_md)
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
    for label, skills_dir in search_roots:
        if not skills_dir.exists():
            continue
        entries = sorted(p for p in skills_dir.iterdir()
                         if p.is_dir() and (p / "skill.md").exists())
        if not entries:
            continue
        print(f"\n{label}  ({skills_dir})")
        for skill_dir in entries:
            name = skill_dir.name
            fm, _ = _read_skill_md(skill_dir / "skill.md")
            desc = (fm.get("description") or "").strip().splitlines()[0] if fm.get("description") else ""
            shadowed = " [shadowed]" if name in seen else ""
            desc_str = f"  — {desc}" if desc else ""
            print(f"  {name}{desc_str}{shadowed}")
            seen.add(name)
        found_any = True

    if not found_any:
        print("No skills found.")
    print()
    print("Run 'reyn skills <name>' for usage details.")
