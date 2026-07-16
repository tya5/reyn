"""Tool descriptions for the ``skill`` bucket.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): the two skill install verbs from
``tools/skill_verbs.py`` (#2548 PR-C / PR-D) — ``skill_install_local``
(register a local ``SKILL.md`` directory) and ``skill_install_source``
(fetch + install from a git/GitHub URL). Each ``.text`` value is copied
verbatim from its origin constant; the origin module now aliases its
``_SKILL_INSTALL_*_DESCRIPTION`` constants to ``skill.NAME.text``.

#2971 adds a third, ``skill_list`` — the read-only discovery verb. Its text
is NOT a relocation (the tool is new), and it carries one load-bearing
instruction the other two do not need: how to actually USE a listed skill.
There is no ``run_skill`` op, by design — a skill body is instructions for
the model, so reading the file with the ordinary ``file`` read op IS the
invocation. The description says so explicitly, because a model that gets a
list of paths and no stated next step is the exact shape of the reachability
gap #2971 exists to close.

Note: both carry ``ToolDefinition.category="io"`` — this module groups
them by feature-area (skill management), matching the ``mcp`` / ``io``
precedent set in Phase 2 (module grouping is conceptual, not a literal
mirror of the ``category`` field).
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

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

skill_list = ToolDescription(
    tool_name="skill_list",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Discovery surface for skills that are registered but not "
        "advertised in the system-prompt menu — returns each one's name, "
        "description, and file path so the model can read the body it "
        "needs (#2971)."
    ),
    text=(
        "List the skills registered in this session that you are allowed to "
        "see, with each skill's name, one-line description, and file path. "
        "Some skills are already listed in the Skills section of your system "
        "prompt; this tool additionally returns on-demand skills, which are "
        "registered and usable but deliberately not advertised there. Call it "
        "when a task looks like it might have a matching skill and the menu "
        "does not show one. To use a skill from the result, read its 'path' "
        "with the file read tool and follow the instructions in the file — "
        "there is no separate run tool. Reading a skill's file is what "
        "invokes it."
    ),
    ja=(
        "このセッションで参照可能な skill の一覧を返す（name / description / "
        "path）。システムプロンプトの '## Skills' に載らない on_demand の "
        "skill もここには現れる。使うときは path を file read ツールで読み、"
        "その内容に従う（専用の実行ツールは無い）。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "skill_install_local": skill_install_local,
    "skill_install_source": skill_install_source,
    "skill_list": skill_list,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────

_name_key_desc = ParamDescription(
    text=(
        "Config key written under skills.entries.<name>. "
        "When omitted, the frontmatter 'name:' field is used; "
        "if that is also absent, the directory basename is used."
    ),
    ja=(
        "skills.entries.<name> に書き込まれる設定キー。省略時は"
        "フロントマターの 'name:' フィールドを使い、それも無ければ"
        "ディレクトリのベース名を使う。"
    ),
)

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "skill_install_local": {
        "path": ParamDescription(
            text=(
                "Path to the skill directory (containing SKILL.md) or "
                "the direct path to the SKILL.md file. May be absolute "
                "or project-root-relative."
            ),
            ja=(
                "SKILL.md を含むスキルディレクトリへのパス、または SKILL.md "
                "ファイルへの直接パス。絶対パスまたはプロジェクトルート相対。"
            ),
        ),
        "name": _name_key_desc,
    },
    "skill_install_source": {
        "source": ParamDescription(
            text=(
                "Git or GitHub URL of the skill repo. The root (or subdir "
                "specified via '//' separator) must contain a SKILL.md file. "
                "Examples: 'https://github.com/user/skill-repo' or "
                "'https://github.com/user/monorepo//skills/my-skill'."
            ),
            ja=(
                "スキルリポジトリの Git/GitHub URL。ルート（または '//' "
                "区切りで指定したサブディレクトリ）に SKILL.md が必要。例 "
                "'https://github.com/user/skill-repo' や "
                "'https://github.com/user/monorepo//skills/my-skill'。"
            ),
        ),
        "name": ParamDescription(
            text=(
                "Config key written under skills.entries.<name>. "
                "When omitted, the frontmatter 'name:' field is used; "
                "if that is also absent, the repo/subdir basename is used."
            ),
            ja=(
                "skills.entries.<name> に書き込まれる設定キー。省略時は"
                "フロントマターの 'name:' フィールドを使い、それも無ければ"
                "repo/subdir のベース名を使う。"
            ),
        ),
    },
}
