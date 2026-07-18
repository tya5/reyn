"""Tool descriptions for the ``io`` category.

Phase 2 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): every ``io``-category
ToolDefinition's description string lives here as a reviewable
``ToolDescription`` record. Each ``.text`` value is copied verbatim from
its origin tool module; the origin module now aliases its
``_X_DESCRIPTION`` module constant to ``io.NAME.text`` so every call
site is unchanged.

Covers: file.py's 6 verbs (read_file / write_file / delete_file /
edit_file / list_directory / grep_files / glob_files), drop_source, and
index_update. ``index_update``'s ``ToolDefinition.category`` field is
``"discovery"`` in code (it shares the FP-0057 index lifecycle with
``drop_source``) — it is grouped here by feature (index/file-adjacent
I/O), matching the Phase 2 dispatch brief, not by its literal
``category=`` value.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

read_file = ToolDescription(
    tool_name="read_file",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Read a file's contents under the agent's read scope, with "
        "guidance toward conventional project-root file locations."
    ),
    text=(
        "Read a file's contents under the agent's read scope. "
        "Common conventions: README is at project root as "
        "`README.md`. CLAUDE.md, CHANGELOG.md, and "
        "configuration files (e.g. `reyn.yaml`, "
        "`pyproject.toml`) are at project root. Try these "
        "conventional paths directly instead of asking the "
        "user where the file lives."
    ),
    ja=(
        "エージェントの読み取りスコープ内のファイル内容を読む。README は "
        "プロジェクトルートの README.md、CLAUDE.md / CHANGELOG.md / 設定"
        "ファイル（reyn.yaml、pyproject.toml 等）もプロジェクトルートにある"
        "という慣習を踏まえ、ユーザーに場所を尋ねる前にこれらの慣習パスを"
        "直接試す。"
    ),
)

write_file = ToolDescription(
    tool_name="write_file",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Create or overwrite a whole file under the agent's write scope; "
        "steers the LLM toward edit_file for partial changes."
    ),
    text=(
        "Write content to a file under the agent's write scope. "
        "Creates or overwrites the WHOLE file. For a partial or surgical "
        "change to an existing file, prefer the `file__edit` action instead of "
        "rewriting the whole file."
    ),
    ja=(
        "エージェントの書き込みスコープ内のファイルにコンテンツを書き込む。"
        "ファイル全体を新規作成または上書きする。既存ファイルへの部分的な"
        "変更には、ファイル全体を書き直すのではなく file__edit を使うことを"
        "推奨する。"
    ),
)

delete_file = ToolDescription(
    tool_name="delete_file",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose="Delete a file under the agent's write scope.",
    text="Delete a file under the agent's write scope.",
    ja="エージェントの書き込みスコープ内のファイルを削除する。",
)

edit_file = ToolDescription(
    tool_name="edit_file",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Replace a unique string in an existing file for a partial/surgical "
        "edit, avoiding a whole-file read+write round-trip."
    ),
    text=(
        "Replace a unique string in a file under the agent's write scope. "
        "`old_string` MUST appear exactly once in the file; if it appears "
        "multiple times, the call fails with a count — re-call with a longer "
        "context-including snippet, or pass `replace_all=true` to replace "
        "every occurrence. Use this for partial edits instead of read+write "
        "for the whole file."
    ),
    ja=(
        "エージェントの書き込みスコープ内のファイルで、一意な文字列を置換"
        "する。old_string はファイル内にちょうど1回だけ出現する必要があり、"
        "複数回出現する場合はエラーになる（より長い文脈を含めて再指定する"
        "か、replace_all=true を渡す）。ファイル全体の読み書きではなく部分"
        "編集に使う。"
    ),
)

grep_files = ToolDescription(
    tool_name="grep_files",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Search for a regex pattern across files under the agent's read "
        "scope, distinct from list_directory's name-only enumeration."
    ),
    text=(
        "Search for a regex pattern across files under the agent's read scope. "
        "Use this when you need to find text or code patterns in files — "
        "do NOT use list_directory for grep/glob intent. "
        "Returns matching lines with file paths and line numbers."
    ),
    ja=(
        "エージェントの読み取りスコープ内のファイルに対して正規表現パター"
        "ンで検索する。テキストやコードパターンを探す際に使う（list_"
        "directory はグレップ/グロブ用途には使わない）。マッチした行を"
        "ファイルパスと行番号付きで返す。"
    ),
)

glob_files = ToolDescription(
    tool_name="glob_files",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Enumerate files by name/glob pattern under the agent's read scope, "
        "distinct from list_directory's flat single-level listing."
    ),
    text=(
        "Find files matching a glob pattern (e.g. '**/*.py') under the agent's "
        "read scope. Use `**` to recurse into subdirectories. Use this when you "
        "need to enumerate files by name pattern — do NOT use list_directory "
        "for glob intent. Returns a list of matching file paths."
    ),
    ja=(
        "エージェントの読み取りスコープ内で glob パターン（例: '**/*.py'）"
        "に一致するファイルを探す。`**` でサブディレクトリを再帰する。"
        "ファイル名パターンでの列挙に使う（list_directory はグロブ用途には"
        "使わない）。一致したファイルパスのリストを返す。"
    ),
)

list_directory = ToolDescription(
    tool_name="list_directory",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "List a single directory's immediate contents (names + types) "
        "under the agent's read scope — the flat, non-recursive counterpart "
        "to grep_files / glob_files."
    ),
    text=(
        "List contents of a directory under the agent's read scope. "
        "Returns names + types (file/dir)."
    ),
    ja=(
        "エージェントの読み取りスコープ内のディレクトリの内容を一覧表示"
        "する。名前と種別（file/dir）を返す。"
    ),
)

drop_source = ToolDescription(
    tool_name="drop_source",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Remove an indexed source entirely (SQLite backend + manifest "
        "entry) — the destructive counterpart to index_update, e.g. when "
        "retiring a trial source or rebuilding from scratch."
    ),
    text=(
        "Remove an indexed source entirely (= delete its SQLite + manifest entry). "
        "Use when retiring trial sources or replacing with a different strategy. "
        "Permission-gated; user is prompted to confirm."
    ),
    ja=(
        "インデックス済みソースを完全に削除する（SQLite バックエンド＋"
        "マニフェストエントリを削除）。試験的なソースの廃止や別の戦略への"
        "切り替え時に使う。パーミッションゲート付きで、ユーザーに確認を"
        "求める。"
    ),
)

index_update = ToolDescription(
    tool_name="index_update",
    surfaced=(
        "router + phase (gates.router=allow, gates.phase=allow) — own-write, "
        "default-ALLOW op (FP-0057 Phase 2a)"
    ),
    purpose=(
        "Incrementally reconcile chunks into an indexed source's in-core "
        "IndexBackend (add/update/remove/skip by content_hash), the "
        "constructive counterpart to drop_source — NO full-rebuild mode."
    ),
    text=(
        "Incrementally ingest chunks into an indexed source, reconciling "
        "against its current content: NEW content_hash values are embedded and "
        "added; a source_path whose chunks changed (new hash under a path "
        "already indexed) is updated (old chunks for that path replaced); a "
        "source_path this call re-supplies chunks for but whose old chunk "
        "hashes are absent from this call are removed; unchanged content_hash "
        "values are skipped (no re-embed). NO full-rebuild mode — to rebuild a "
        "source from scratch, call `drop_source` then `index_update` on the "
        "fresh (empty) source. The caller supplies pre-chunked text (chunking "
        "is the caller's responsibility, not this tool's)."
    ),
    ja=(
        "インデックス済みソースに対してチャンクを差分投入し、現在の内容と"
        "照合する（reconcile）: 新しい content_hash は埋め込んで追加、既存"
        "パスのハッシュが変わっていれば更新、今回再提示されなかった旧"
        "ハッシュは削除、変化のない content_hash は再埋め込みせずスキップ"
        "する。フルリビルドモードはない（ゼロから作り直すには drop_source "
        "してから空のソースに index_update する）。チャンク分割は呼び出し"
        "側の責任。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "read_file": read_file,
    "write_file": write_file,
    "delete_file": delete_file,
    "edit_file": edit_file,
    "grep_files": grep_files,
    "glob_files": glob_files,
    "list_directory": list_directory,
    "drop_source": drop_source,
    "index_update": index_update,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────
#
# Only fields that actually declare a "description" in the origin
# ``parameters`` JSON-schema get an entry here — many params (e.g. `path`
# on read_file) are bare `{"type": "string"}` with no description.

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "read_file": {
        "offset": ParamDescription(
            text=(
                "Line number to start reading from (0-indexed). "
                "Omit to start at the beginning of the file."
            ),
            ja="読み取り開始行（0始まり）。省略時はファイル先頭から。",
        ),
        "limit": ParamDescription(
            text=(
                "Number of lines to read from `offset`. "
                "Omit to read through end of file."
            ),
            ja="`offset` から読む行数。省略時はファイル末尾まで読む。",
        ),
    },
    "edit_file": {
        "old_string": ParamDescription(
            text=(
                "Exact text to replace. Must appear exactly once unless "
                "replace_all is true; include surrounding context to "
                "make it unique."
            ),
            ja=(
                "置換対象の完全一致テキスト。replace_all=true でない限り"
                "ファイル内に1回だけ出現する必要があるので、周辺文脈を含めて"
                "一意にする。"
            ),
        ),
        "new_string": ParamDescription(
            text="Replacement text.",
            ja="置換後のテキスト。",
        ),
        "replace_all": ParamDescription(
            text=(
                "When true, every occurrence of old_string is replaced. "
                "Default false (= require uniqueness)."
            ),
            ja="true なら old_string の全出現を置換。デフォルト false（一意性を要求）。",
        ),
    },
    "grep_files": {
        "pattern": ParamDescription(
            text="Regex pattern to search for.",
            ja="検索する正規表現パターン。",
        ),
        "path": ParamDescription(
            text="Directory or file to search. Defaults to '.' (workspace root).",
            ja="検索対象のディレクトリまたはファイル。デフォルトは '.'（ワークスペースルート）。",
        ),
        "glob": ParamDescription(
            text="File-glob filter (e.g. '**/*.py'). Searches all files when omitted.",
            ja="ファイル glob フィルタ（例 '**/*.py'）。省略時は全ファイルを検索。",
        ),
        "case_sensitive": ParamDescription(
            text="When true, search is case-sensitive. Defaults to false.",
            ja="true なら大文字小文字を区別。デフォルト false。",
        ),
        "max_results": ParamDescription(
            text="Maximum number of matches to return. Defaults to 50.",
            ja="返す一致件数の上限。デフォルト 50。",
        ),
    },
    "glob_files": {
        "pattern": ParamDescription(
            text=(
                "Glob pattern. To match by name anywhere under a directory, "
                "always include `**` (e.g. '**/*.py' or 'src/**/*.md'). "
                "A bare name without `**` matches only at the exact root "
                "level, not recursively."
            ),
            ja=(
                "glob パターン。ディレクトリ配下のどこでも名前一致させたいなら"
                "必ず `**` を含める（例 '**/*.py'）。`**` のない裸の名前は"
                "ルート直下のみ一致し再帰しない。"
            ),
        ),
        "path": ParamDescription(
            text="Root directory for the glob. Defaults to '.' (workspace root).",
            ja="glob 検索の起点ディレクトリ。デフォルトは '.'（ワークスペースルート）。",
        ),
        "max_results": ParamDescription(
            text=(
                "Maximum number of matching paths to return. Defaults to 50 — "
                "raise this explicitly when enumerating a directory that may "
                "hold more entries than that (e.g. bulk ingestion), otherwise "
                "matches beyond the cap are silently dropped."
            ),
            ja=(
                "返す一致パス件数の上限。デフォルト 50 — それを超える件数が"
                "見込まれるディレクトリを列挙する場合（一括取り込み等）は"
                "明示的に引き上げること。さもないと上限を超えた一致は"
                "黙って切り捨てられる。"
            ),
        ),
        "absolute": ParamDescription(
            text=(
                "When true, return absolute paths even for a relative "
                "pattern (default false = project-relative paths). Set "
                "this when the result feeds something that needs an "
                "absolute path regardless of the pattern's own form (e.g. "
                "a file:// URI) — do not try to reconstruct an absolute "
                "path from a relative match yourself."
            ),
            ja=(
                "true なら相対パターンでも絶対パスを返す（デフォルト false"
                "＝プロジェクト相対パス）。パターン自体が相対かどうかに"
                "関わらず絶対パスが必要な用途（file:// URI 構築等）で"
                "使うこと — 相対一致から絶対パスを自前で組み立てようと"
                "しないこと。"
            ),
        ),
    },
    "list_directory": {
        "max_results": ParamDescription(
            text=(
                "Maximum number of directory entries to return. Defaults to "
                "50 — raise this explicitly for directories that may hold "
                "more entries than that, otherwise entries beyond the cap "
                "are silently dropped."
            ),
            ja=(
                "返すディレクトリエントリ件数の上限。デフォルト 50 — それを"
                "超える件数が見込まれるディレクトリでは明示的に引き上げる"
                "こと。さもないと上限を超えたエントリは黙って切り捨てられる。"
            ),
        ),
    },
    "drop_source": {
        "source": ParamDescription(
            text="Logical source name to remove (from list_rag_sources).",
            ja="削除する論理ソース名（list_rag_sources の結果から）。",
        ),
    },
    "index_update": {
        "source": ParamDescription(
            text="Logical source name to ingest into.",
            ja="投入先の論理ソース名。",
        ),
        "chunks": ParamDescription(
            text="Chunks to reconcile into the index.",
            ja="インデックスに反映するチャンク群。",
        ),
        "chunks.metadata": ParamDescription(
            text=(
                "content_hash (required, change-detection key), "
                "source_path (required, reconciliation-scope "
                "key), plus optional source_type / chunk_index "
                "/ size_tokens / parent_context / extra."
            ),
            ja=(
                "content_hash（必須、変更検知キー）、source_path（必須、"
                "reconcile 範囲キー）、任意で source_type / chunk_index / "
                "size_tokens / parent_context / extra。"
            ),
        ),
        "embedding_model": ParamDescription(
            text=(
                "Embedding model class, used ONLY when this source has no "
                "recorded model yet (first index_update for a new source) "
                "— an already-indexed source's recorded model always wins "
                "(a source is one embedding space)."
            ),
            ja=(
                "埋め込みモデルクラス。このソースに記録済みモデルがまだない"
                "場合（新規ソースの最初の index_update）のみ使用される — "
                "既にインデックス済みのソースは記録済みモデルが常に優先"
                "（1ソース=1埋め込み空間）。"
            ),
        ),
        "description": ParamDescription(
            text="SourceManifest description (first-index or override).",
            ja="SourceManifest の説明文（初回インデックス時または上書き）。",
        ),
        "path": ParamDescription(
            text="SourceManifest path label (first-index or override).",
            ja="SourceManifest のパスラベル（初回インデックス時または上書き）。",
        ),
    },
}
