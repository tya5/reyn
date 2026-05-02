"""/skills slash command."""
from __future__ import annotations

from reyn.chat.slash import REGISTRY, SlashCommand


async def _handle_skills(session: "object", args: str) -> None:
    """List available skills resolved via the skill path resolver."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.skill.skill_paths import resolve_skill_path, stdlib_root
    from pathlib import Path
    import os

    lines: list[str] = ["available skills:"]

    # Stdlib skills
    stdlib = stdlib_root()
    if stdlib.is_dir():
        stdlib_names = sorted(
            d.name for d in stdlib.iterdir()
            if d.is_dir() and (d / "skill.md").is_file()
        )
        if stdlib_names:
            lines.append("  stdlib: " + ", ".join(stdlib_names))

    # Project skills
    project_root = Path.cwd()
    project_skills_dir = project_root / "reyn" / "project"
    if project_skills_dir.is_dir():
        project_names = sorted(
            d.name for d in project_skills_dir.iterdir()
            if d.is_dir() and (d / "skill.md").is_file()
        )
        if project_names:
            lines.append("  project: " + ", ".join(project_names))

    # Local skills
    local_skills_dir = project_root / "reyn" / "local"
    if local_skills_dir.is_dir():
        local_names = sorted(
            d.name for d in local_skills_dir.iterdir()
            if d.is_dir() and (d / "skill.md").is_file()
        )
        if local_names:
            lines.append("  local: " + ", ".join(local_names))

    if len(lines) == 1:
        lines.append("  (none found)")

    await session._put_outbox(OutboxMessage(
        kind="status",
        text="\n".join(lines),
    ))


REGISTRY.register(SlashCommand(
    name="skills",
    summary="List available skills (stdlib, project, local)",
    handler=_handle_skills,
))
