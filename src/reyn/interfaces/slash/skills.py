"""/skills slash command."""
from __future__ import annotations

import textwrap
from pathlib import Path

from reyn.interfaces.slash import reply, slash

# See help.py for rationale — 65 = common 80-col terminal minus the
# combined body indent (7) + conv pane / RichLog padding overhead.
_TARGET_WIDTH = 65


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
    # list, so /skills hid every shipped skill (eval,
    # direct_llm, skill_router, …). PR-N3: chat_compactor retired.
    sources: list[tuple[str, list[str]]] = [
        ("stdlib", _list_skills(stdlib_root() / "skills")),
        ("project", _list_skills(project_root / "reyn" / "project")),
        ("local", _list_skills(project_root / "reyn" / "local")),
    ]

    lines: list[str] = ["available skills:"]
    for label, names in sources:
        if not names:
            continue
        prefix = f"  {label}: "
        body = ", ".join(names)
        if len(prefix) + len(body) <= _TARGET_WIDTH:
            lines.append(prefix + body)
        else:
            # Hanging indent: wrap continuations under the comma-list so
            # the source label stays anchored and the long list doesn't
            # break to column 0 mid-word.
            wrapped = textwrap.fill(
                body,
                width=_TARGET_WIDTH,
                initial_indent=prefix,
                subsequent_indent=" " * len(prefix),
                break_long_words=False,
                break_on_hyphens=False,
            )
            lines.append(wrapped)
    if len(lines) == 1:
        lines.append("  (none found)")

    await reply(session, "\n".join(lines))
