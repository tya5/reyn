"""Tool descriptions for the ``skill`` bucket.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): the two skill install verbs from
``tools/skill_verbs.py`` (#2548 PR-C / PR-D) — ``skill_install_local``
(register a local ``SKILL.md`` directory) and ``skill_install_source``
(fetch + install from a git/GitHub URL). Each ``.text`` value is copied
verbatim from its origin constant; the origin module now aliases its
``_SKILL_INSTALL_*_DESCRIPTION`` constants to ``skill.NAME.text``.

Note: both carry ``ToolDefinition.category="io"`` — this module groups
them by feature-area (skill management), matching the ``mcp`` / ``io``
precedent set in Phase 2 (module grouping is conceptual, not a literal
mirror of the ``category`` field).
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ToolDescription

skill_install_local = ToolDescription(
    tool_name="skill_install_local",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Register a local skill directory (SKILL.md) into the project "
        "config so it becomes available to sessions after the next "
        "hot-reload."
    ),
    text=(
        "Register a local skill directory into the project config "
        "by reading its SKILL.md frontmatter and writing an entry to "
        ".reyn/config/skills.yaml. The skill is immediately available "
        "to sessions after the next hot-reload. Pass the path to the "
        "directory containing SKILL.md (or the SKILL.md file directly). "
        "Use 'name' to override the config key when the directory name "
        "differs from the desired skill identifier."
    ),
    ja=(
        "ローカルのスキルディレクトリをプロジェクト設定に登録する"
        "（SKILL.md のフロントマターを読み、.reyn/config/skills.yaml に"
        "エントリを書き込む）。次のホットリロード後、セッションから即座"
        "に利用可能になる。"
    ),
)

skill_install_source = ToolDescription(
    tool_name="skill_install_source",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Fetch a skill from a git/GitHub URL, shallow-clone + "
        "threat-scan its SKILL.md, and install it into the project config."
    ),
    text=(
        "Fetch a skill from a git/GitHub URL and install it into the project. "
        "The repo is shallow-cloned to .reyn/skills/<name>/, the SKILL.md is "
        "threat-scanned, and an entry is written to .reyn/config/skills.yaml. "
        "The skill is immediately available to sessions after the next hot-reload. "
        "Requires http.get permission for the source host in the skill's frontmatter. "
        "Source format: 'https://github.com/user/repo' (repo root must contain SKILL.md) "
        "or 'https://github.com/user/repo//path/to/skill' (subdir with SKILL.md). "
        "Use 'name' to override the config key when the default (from SKILL.md frontmatter "
        "or repo/subdir basename) differs from the desired skill identifier."
    ),
    ja=(
        "git/GitHub の URL からスキルを取得しプロジェクトにインストール"
        "する。リポジトリは .reyn/skills/<name>/ に浅くクローンされ、"
        "SKILL.md は脅威スキャンされた上で .reyn/config/skills.yaml にエ"
        "ントリが書き込まれる。ソースホストへの http.get 権限が必要。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "skill_install_local": skill_install_local,
    "skill_install_source": skill_install_source,
}
