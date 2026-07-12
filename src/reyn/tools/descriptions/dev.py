"""Tool descriptions for the ``dev`` category.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): the four ``reyn_src_*``
router-only dev-mode tools (ADR-0026 M3 Wave 1) that browse Reyn's own
repository — ``reyn_src_list`` / ``reyn_src_read`` / ``reyn_src_glob`` /
``reyn_src_grep``. Each ``.text`` value is copied verbatim from
``tools/reyn_src.py``; the origin module now aliases its
``_REYN_SRC_*_DESCRIPTION`` constants to ``dev.NAME.text``.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ToolDescription

reyn_src_list = ToolDescription(
    tool_name="reyn_src_list",
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

reyn_src_read = ToolDescription(
    tool_name="reyn_src_read",
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
        "entry point: reyn_src_read(\"README.md\") for the overview + "
        "curated map of deep-dive paths."
    ),
    ja=(
        "Reyn 自身のリポジトリ内のテキストファイルを、リポジトリルート"
        "からの正確な相対パスで読む。ユーザーが名指ししたファイル（例: "
        "README.md）を読む場合、またはインデックス済みソースがそのト"
        "ピックをカバーしていない場合のナビゲーションに使う。"
    ),
)

reyn_src_glob = ToolDescription(
    tool_name="reyn_src_glob",
    surfaced="router-only (gates.router=allow, gates.phase=deny)",
    purpose=(
        "Enumerate files in Reyn's own repository by structural glob "
        "pattern, distinct from content search (reyn_src_grep) and "
        "single-file read (reyn_src_read)."
    ),
    text=(
        "Find files in Reyn's own repository by glob pattern (e.g. "
        "'docs/**/*.md', 'src/**/router*.py'). Returns up to 200 "
        "repo-root-relative paths, alphabetically sorted. Use this when "
        "you need to enumerate files matching a structural pattern; for "
        "content search use reyn_src_grep, for a single named file use "
        "reyn_src_read."
    ),
    ja=(
        "Reyn 自身のリポジトリ内のファイルを glob パターン（例: "
        "'docs/**/*.md'）で検索する。最大200件のリポジトリルート相対パ"
        "スをアルファベット順に返す。構造的パターンでファイルを列挙し"
        "たい場合に使う。内容検索には reyn_src_grep、単一ファイルの読"
        "み込みには reyn_src_read を使う。"
    ),
)

reyn_src_grep = ToolDescription(
    tool_name="reyn_src_grep",
    surfaced="router-only (gates.router=allow, gates.phase=deny)",
    purpose=(
        "Search Reyn's own repository contents by regex, for 'where is X "
        "handled in the source' style questions, distinct from structural "
        "enumeration (reyn_src_glob) and single-file read (reyn_src_read)."
    ),
    text=(
        "Search file contents in Reyn's own repository by regex. Returns "
        "up to 50 matches as {path, line, snippet}. `path` scopes the "
        "search (default = whole repo); `glob` further narrows by filename "
        "(e.g. '**/*.py'). Use this for 'where in the Reyn source is X "
        "handled' style questions; for structural enumeration use "
        "reyn_src_glob, for reading one known file use reyn_src_read."
    ),
    ja=(
        "Reyn 自身のリポジトリ内のファイル内容を正規表現で検索する。最"
        "大50件の一致を {path, line, snippet} として返す。'X はソース"
        "のどこで処理されているか' 式の質問に使う。構造的な列挙には "
        "reyn_src_glob、既知の1ファイルの読み込みには reyn_src_read を"
        "使う。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "reyn_src_list": reyn_src_list,
    "reyn_src_read": reyn_src_read,
    "reyn_src_glob": reyn_src_glob,
    "reyn_src_grep": reyn_src_grep,
}
