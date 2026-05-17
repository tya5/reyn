"""/skills slash command."""
from __future__ import annotations

from pathlib import Path

from reyn.chat.slash import reply, slash


def _list_skills(root: Path) -> list[str]:
    """Return sorted skill names under `root` (each `<name>/skill.md`)."""
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and (d / "skill.md").is_file()
    )


@slash("skills", summary="List available skills (stdlib, project, local)")
async def skills_cmd(session: "object", args: str) -> None:
    from reyn.skill.skill_paths import stdlib_root

    project_root = Path.cwd()
    # stdlib skills live under <stdlib_root>/skills/<name>/skill.md, not
    # directly under <stdlib_root>. The old path always produced an empty
    # list, so /skills hid every shipped skill (chat_compactor, eval,
    # direct_llm, skill_router, …).
    sources: list[tuple[str, list[str]]] = [
        ("stdlib", _list_skills(stdlib_root() / "skills")),
        ("project", _list_skills(project_root / "reyn" / "project")),
        ("local", _list_skills(project_root / "reyn" / "local")),
    ]

    lines: list[str] = ["available skills:"]
    for label, names in sources:
        if names:
            lines.append(f"  {label}: " + ", ".join(names))
    if len(lines) == 1:
        lines.append("  (none found)")

    await reply(session, "\n".join(lines))
