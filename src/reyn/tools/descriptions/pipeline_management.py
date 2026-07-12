"""Tool descriptions for the ``pipeline_management`` bucket.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): the two pipeline install verbs
from ``tools/pipeline_management_verbs.py`` — ``pipeline_install_local``
(register a local DSL file) and ``pipeline_install_source`` (fetch +
install from a git/GitHub URL). Each ``.text`` value is copied verbatim
from its origin constant; the origin module now aliases its
``_PIPELINE_INSTALL_*_DESCRIPTION`` constants to
``pipeline_management.NAME.text``.

Note: both carry ``ToolDefinition.category="io"`` — this module groups
them by feature-area (pipeline management), matching the ``mcp`` / ``io``
precedent set in Phase 2 (module grouping is conceptual, not a literal
mirror of the ``category`` field).
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ToolDescription

pipeline_install_local = ToolDescription(
    tool_name="pipeline_install_local",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Register a local pipeline DSL file into the project config so "
        "sessions can launch it (via pipeline__<key>.<name> or "
        "run_pipeline) after the next hot-reload."
    ),
    text=(
        "Register a local pipeline DSL file into the project config "
        "by writing an entry to .reyn/config/pipelines.yaml. The pipeline is "
        "immediately available to sessions (as pipeline__<key>.<name> and "
        "run_pipeline) after the next hot-reload. Pass the direct path to the "
        "pipeline's *.yaml DSL file (which may hold multiple '---'-separated "
        "'pipeline:' documents). "
        "Use 'name' to set the NAMESPACE KEY for the file; every pipeline in it "
        "registers as '<name>.<declared-pipeline-name>'. 'name' need not match any "
        "declared name and must not contain '.' (the reserved separator); it "
        "defaults to the DSL file stem when omitted."
    ),
    ja=(
        "ローカルのパイプライン DSL ファイルをプロジェクト設定に登録す"
        "る（.reyn/config/pipelines.yaml へのエントリ書き込み）。次の"
        "ホットリロード後、pipeline__<key>.<name> および run_pipeline "
        "としてセッションから即座に利用可能になる。"
    ),
)

pipeline_install_source = ToolDescription(
    tool_name="pipeline_install_source",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Fetch a pipeline from a git/GitHub URL, shallow-clone + "
        "threat-scan it, and install it into the project config."
    ),
    text=(
        "Fetch a pipeline from a git/GitHub URL and install it into the project. "
        "The repo is shallow-cloned to .reyn/pipelines/<name>/, the DSL is parsed "
        "+ threat-scanned, and an entry is written to .reyn/config/pipelines.yaml. "
        "The pipeline is immediately available to sessions after the next "
        "hot-reload. Requires http.get permission for the source host. "
        "Source format: 'https://github.com/user/repo' (repo root must contain "
        "exactly one *.yaml DSL file, or 'path' selects it) or "
        "'https://github.com/user/repo//path/to/pipelines' (subdir form). "
        "Use 'path' to select the DSL file inside the clone when the repo/subdir "
        "contains more than one *.yaml file. "
        "Use 'name' to set the NAMESPACE KEY; every pipeline in the file registers "
        "as '<name>.<declared-pipeline-name>'. 'name' need not match any declared "
        "name and must not contain '.'; it defaults to the source basename."
    ),
    ja=(
        "git/GitHub の URL からパイプラインを取得しプロジェクトにインス"
        "トールする。リポジトリは .reyn/pipelines/<name>/ に浅くクロー"
        "ンされ、DSL はパース + 脅威スキャンされた上で "
        ".reyn/config/pipelines.yaml にエントリが書き込まれる。ソースホ"
        "ストへの http.get 権限が必要。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "pipeline_install_local": pipeline_install_local,
    "pipeline_install_source": pipeline_install_source,
}
