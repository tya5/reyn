"""Tool descriptions for the ``dev`` category.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): the four ``reyn_repo_*``
router-only dev-mode tools (ADR-0026 M3 Wave 1) that browse Reyn's own
repository — ``reyn_repo_list`` / ``reyn_repo_read`` / ``reyn_repo_glob`` /
``reyn_repo_grep``. Each ``.text`` value is copied verbatim from
``tools/reyn_repo.py``; the origin module now aliases its
``_REYN_REPO_*_DESCRIPTION`` constants to ``dev.NAME.text``.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

reyn_repo_list = ToolDescription(
    tool_name="reyn_repo_list",
    surfaced="router-only (gates.router=allow, gates.phase=deny)",
    purpose=(
        "Discover Reyn's own source/doc layout (repo root or a "
        "subdirectory) before reading specific files."
    ),
    text=(
        "List entries under a path inside Reyn's own repository "
        "(= the project that built this agent). Pass \"\" for "
        "the repo root. Returns names + types (file/dir). Use "
        "this to discover Reyn's source/doc layout before "
        "reading specific files. Examples: list \"\" for the "
        "top-level layout, \"docs/concepts\" for concept docs "
        "(English; Japanese translations are the same path with a "
        "\".ja.md\" filename suffix, not a separate directory), or "
        "any subdirectory path for its contents."
    ),
    ja=(
        "Reyn 自身のリポジトリ（このエージェントを構築したプロジェクト）"
        "内のパス配下のエントリを一覧する。\"\" でリポジトリルート。"
        "名前とタイプ（file/dir）を返す。特定ファイルを読む前にレイアウ"
        "トを把握するために使う。"
    ),
)

reyn_repo_read = ToolDescription(
    tool_name="reyn_repo_read",
    surfaced="router-only (gates.router=allow, gates.phase=deny)",
    purpose=(
        "Read one named file from Reyn's own repository by exact path, "
        "when no indexed source covers the topic (else prefer "
        "semantic_search)."
    ),
    text=(
        "Read a text file from Reyn's own repository by an exact "
        "repo-root-relative path. Use for: (a) reading a specific file the "
        "user named (e.g. README.md), or (b) navigating "
        "Reyn's source / docs when NO indexed source covers the topic. "
        "If an indexed source description mentions concepts / design / "
        "docs / Reyn, use `semantic_search` instead — guessing a file path is "
        "unreliable; semantic search over indexed chunks is not. Fallback "
        "entry point: reyn_repo_read(\"README.md\") for the overview + "
        "curated map of deep-dive paths."
    ),
    ja=(
        "Reyn 自身のリポジトリ内のテキストファイルを、リポジトリルート"
        "からの正確な相対パスで読む。ユーザーが名指ししたファイル（例: "
        "README.md）を読む場合、またはインデックス済みソースがそのト"
        "ピックをカバーしていない場合のナビゲーションに使う。"
    ),
)

reyn_repo_glob = ToolDescription(
    tool_name="reyn_repo_glob",
    surfaced="router-only (gates.router=allow, gates.phase=deny)",
    purpose=(
        "Enumerate files in Reyn's own repository by structural glob "
        "pattern, distinct from content search (reyn_repo_grep) and "
        "single-file read (reyn_repo_read)."
    ),
    text=(
        "Find files in Reyn's own repository by glob pattern (e.g. "
        "'docs/**/*.md', 'src/**/router*.py'). Returns up to 200 "
        "repo-root-relative paths, alphabetically sorted. Use this when "
        "you need to enumerate files matching a structural pattern; for "
        "content search use reyn_repo_grep, for a single named file use "
        "reyn_repo_read."
    ),
    ja=(
        "Reyn 自身のリポジトリ内のファイルを glob パターン（例: "
        "'docs/**/*.md'）で検索する。最大200件のリポジトリルート相対パ"
        "スをアルファベット順に返す。構造的パターンでファイルを列挙し"
        "たい場合に使う。内容検索には reyn_repo_grep、単一ファイルの読"
        "み込みには reyn_repo_read を使う。"
    ),
)

reyn_repo_grep = ToolDescription(
    tool_name="reyn_repo_grep",
    surfaced="router-only (gates.router=allow, gates.phase=deny)",
    purpose=(
        "Search Reyn's own repository contents by regex, for 'where is X "
        "handled in the source' style questions, distinct from structural "
        "enumeration (reyn_repo_glob) and single-file read (reyn_repo_read)."
    ),
    text=(
        "Search file contents in Reyn's own repository by regex. Returns "
        "up to 50 matches as {path, line, snippet}. `path` scopes the "
        "search (default = whole repo); `glob` further narrows by filename "
        "(e.g. '**/*.py'). Use this for 'where in the Reyn source is X "
        "handled' style questions; for structural enumeration use "
        "reyn_repo_glob, for reading one known file use reyn_repo_read."
    ),
    ja=(
        "Reyn 自身のリポジトリ内のファイル内容を正規表現で検索する。最"
        "大50件の一致を {path, line, snippet} として返す。'X はソース"
        "のどこで処理されているか' 式の質問に使う。構造的な列挙には "
        "reyn_repo_glob、既知の1ファイルの読み込みには reyn_repo_read を"
        "使う。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "reyn_repo_list": reyn_repo_list,
    "reyn_repo_read": reyn_repo_read,
    "reyn_repo_glob": reyn_repo_glob,
    "reyn_repo_grep": reyn_repo_grep,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────
#
# reyn_repo_list has no param-level description (path is bare-typed) — no
# entry needed.

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "reyn_repo_read": {
        "offset": ParamDescription(
            text=(
                "Line number to start reading from (0-indexed). "
                "Omit to start at the beginning of the file. When set "
                "(with or without limit), the 256-KB byte cap is "
                "bypassed by line-streaming only the requested slice."
            ),
            ja=(
                "読み取り開始行（0始まり）。省略時はファイル先頭から。"
                "設定時（limit有無に関わらず）は256KBのバイト上限を"
                "回避し、要求された範囲のみを行単位でストリームする。"
            ),
        ),
        "limit": ParamDescription(
            text=(
                "Number of lines to read from `offset`. "
                "Omit to read through end of file."
            ),
            ja="`offset` から読む行数。省略時はファイル末尾まで読む。",
        ),
    },
    "reyn_repo_glob": {
        "pattern": ParamDescription(
            text="Glob pattern (e.g. '**/*.py', 'docs/**/*.md').",
            ja="glob パターン（例 '**/*.py', 'docs/**/*.md'）。",
        ),
    },
    "reyn_repo_grep": {
        "pattern": ParamDescription(
            text="Regex pattern (Python `re` syntax).",
            ja="正規表現パターン（Python `re` 構文）。",
        ),
        "path": ParamDescription(
            text=(
                "Repo-relative directory or file to scope the search. "
                "Default = repo root. Use '' for repo root."
            ),
            ja="検索範囲をリポジトリ相対のディレクトリ/ファイルに絞る。デフォルトはリポジトリルート。",
        ),
        "glob": ParamDescription(
            text=(
                "Optional filename glob filter (e.g. '**/*.py'). "
                "When omitted, all text files under `path` are searched."
            ),
            ja="任意のファイル名 glob フィルタ（例 '**/*.py'）。省略時は `path` 配下の全テキストファイルを検索。",
        ),
        "case_sensitive": ParamDescription(
            text="Default false (= case-insensitive).",
            ja="デフォルト false（大文字小文字を区別しない）。",
        ),
        "max_results": ParamDescription(
            text="Cap on match count. Default 50.",
            ja="一致件数の上限。デフォルト 50。",
        ),
    },
}
