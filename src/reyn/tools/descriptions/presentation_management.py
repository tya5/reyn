"""Tool descriptions for the ``presentation_management`` bucket (proposal 0060
Phase 1 Layer A, A8).

The single install verb from ``tools/presentation_management_verbs.py`` —
``presentation_management__install`` (register a named presentation template
into the project config). Mirrors the ``skill`` / ``pipeline_management``
description-package precedent (Phase 3 tool-description package refactor
pattern), applied fresh here since present-install is a new op (no
byte-identical-relocation constraint applies to a first-time description).
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

presentation_install = ToolDescription(
    tool_name="presentation_install_local",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Register a named presentation template (a declarative component "
        "tree) into the project config so a present(view=<name>) op can "
        "resolve it, becoming available to sessions after the next "
        "hot-reload."
    ),
    text=(
        "Register a named presentation template into the project config by "
        "writing an entry to .reyn/config/presentations.yaml. The blueprint "
        "must use only the catalog components (text/markdown/code/diff/"
        "keyvalue/table/list/image) with $bind JSON-Pointer bindings — the "
        "same structural gate an inline present(blueprint=...) already "
        "passes through, so a malformed blueprint is refused before any "
        "config mutation. The template is immediately available to "
        "sessions after the next hot-reload, and renders only when a "
        "present(view=<name>) op names it — installing it never renders "
        "anything on its own."
    ),
    ja=(
        "宣言的なコンポーネントツリー（プレゼンテーションテンプレート）を"
        "名前付きでプロジェクト設定に登録する（.reyn/config/presentations.yaml "
        "にエントリを書き込む）。次のホットリロード後、present(view=<name>) "
        "から即座に解決可能になる。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "presentation_install_local": presentation_install,
}


# ── per-parameter descriptions ────────────────────────────────────────────

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "presentation_install_local": {
        "name": ParamDescription(
            text=(
                "Config key written under presentations.entries.<name> — "
                "the value a present(view=<name>) op resolves against."
            ),
            ja=(
                "presentations.entries.<name> に書き込まれる設定キー。"
                "present(view=<name>) が解決する値。"
            ),
        ),
        "blueprint": ParamDescription(
            text=(
                "The declarative component tree (same shape as an inline "
                "present(blueprint=...) — object or list of catalog "
                "component nodes with $bind JSON-Pointer bindings)."
            ),
            ja=(
                "宣言的なコンポーネントツリー（インラインの "
                "present(blueprint=...) と同じ形— カタログコンポーネント"
                "ノードのオブジェクトまたはリスト、$bind JSON-Pointer "
                "バインディング付き）。"
            ),
        ),
    },
}
