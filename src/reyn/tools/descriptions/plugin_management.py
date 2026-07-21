"""Tool descriptions for the ``plugin_management`` bucket (ADR 0064 P2).

``plugin_install`` / ``plugin_uninstall`` — the LLM-facing surface for the
plugin model's promote/install lifecycle (ADR 0064 §3.2/§3.8/§3.9). One
typed op each (Control IR — ``PluginInstallIROp`` / ``PluginUninstallIROp``
in ``reyn.schemas.models``), a thin verb wrapper in
``tools/plugin_management_verbs.py``.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

plugin_install = ToolDescription(
    tool_name="plugin_management__install",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Promote a just-authored, just-tested capability (an MCP server / "
        "pipeline / skill) into a reusable plugin, or install a pre-existing "
        "one — the same copy+register mechanism from three different sources."
    ),
    text=(
        "Install a plugin — a self-contained directory with a "
        ".reyn-plugin/plugin.json manifest declaring which capabilities "
        "(mcp / pipelines / skills, any subset) it ships. The source is one "
        "of three kinds, pick exactly one field set: "
        "{kind: 'builtin', name: '<name>'} for one of reyn's own shipped "
        "plugins (pass just the name); "
        "{kind: 'local', path: '<dir>'} for a local directory you just "
        "authored/tested (the PRIMARY daily use — 'promote' your own work "
        "into something reusable across sessions/projects); "
        "{kind: 'git', url: '<url>'} for a remote git repository (the "
        "highest-trust-risk source — fetches and can register runnable code "
        "from a remote party; use only for a repo you trust). "
        "The plugin's code is copied to ~/.reyn/plugins/<name>/ (global, "
        "once), its ${REYN_*} location tokens are expanded, any declared "
        "Python dependencies (a requirements.txt at the plugin root) are "
        "installed into a per-plugin virtual environment, and every "
        "capability the manifest declares is registered into this project's "
        "config (the exact same registration a direct skill_install / "
        "pipeline_install / local mcp install performs) — reusable from the "
        "next hot-reload onward. 'name' overrides the manifest's own name as "
        "the install/registry key."
    ),
    ja=(
        "プラグイン（.reyn-plugin/plugin.json マニフェストを持つ自己完結"
        "ディレクトリ）をインストールする。source は builtin/local/git の"
        "いずれか一つ。local が主要な日常フロー（自分が書いてテスト済みの"
        "成果物を再利用可能にする「昇格」）。git は最もリスクが高い（信頼"
        "できるリポジトリにのみ使うこと）。"
    ),
)

plugin_uninstall = ToolDescription(
    tool_name="plugin_management__uninstall",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose="Remove a previously installed plugin — the inverse of plugin_install.",
    text=(
        "Uninstall a plugin previously installed via plugin_install. Removes "
        "every project config entry (mcp / pipelines / skills) the plugin "
        "registered, then removes its ~/.reyn/plugins/<name>/ code copy. "
        "Pass the plugin's install name (the 'name' plugin_install returned, "
        "or the manifest name when no override was given)."
    ),
    ja=(
        "以前 plugin_install でインストールしたプラグインをアンインストール"
        "する。登録した全プロジェクト設定エントリを先に削除し、その後で"
        "~/.reyn/plugins/<name>/ のコードコピーを削除する。"
    ),
)

plugin_list = ToolDescription(
    tool_name="plugin_management__list",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Discover which builtin plugins reyn ships and can install (#3202) "
        "-- the normal-flow answer to 'what can I install', not something "
        "you should have to hit an install error to learn."
    ),
    text=(
        "List every builtin plugin reyn ships and can install. Call this "
        "FIRST whenever the user wants something reyn does not appear to "
        "support yet -- e.g. searching/retrieving over their own documents "
        "-- before concluding it isn't possible: a matching builtin plugin "
        "may exist but simply not be installed yet. Returns each plugin's "
        "name, description, capabilities (any subset of mcp/pipelines/"
        "skills), and the exact call to install it: "
        "plugin_management__install(source={'kind': 'builtin', 'name': "
        "'<name>'}). For example, if 'rag' is listed, install it with "
        "plugin_management__install(source={'kind': 'builtin', 'name': "
        "'rag'})."
    ),
    ja=(
        "reyn が同梱し install 可能な builtin プラグインを一覧する。ユーザ"
        "が今できなそうなことを望んだら(自分の文書を検索/取得したい等)、"
        "「できない」と結論する前にまずこれを呼ぶこと -- 対応する builtin "
        "プラグインが存在するが未 install なだけかもしれない。各プラグイン"
        "の名前・description・capabilities に加え、install する具体的な"
        "呼び出し(plugin_management__install(source={'kind':'builtin', "
        "'name':'<name>'}))を返す。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "plugin_management__install": plugin_install,
    "plugin_management__uninstall": plugin_uninstall,
    "plugin_management__list": plugin_list,
}

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "plugin_management__install": {
        "source": ParamDescription(
            text=(
                "Discriminated union — exactly one of "
                "{kind:'builtin', name}, {kind:'local', path}, "
                "{kind:'git', url}. Pick the kind that matches where the "
                "plugin comes from; do not guess a shape — use the field "
                "names exactly as given for the chosen kind."
            ),
            ja="source の種別ごとのフィールドを厳密に使うこと(推測しない)。",
        ),
        "name": ParamDescription(
            text="Override the manifest's own name as the install/registry key. Optional.",
            ja="マニフェスト名を上書きするインストール名(省略可)。",
        ),
    },
    "plugin_management__uninstall": {
        "name": ParamDescription(
            text="The plugin's install name (as returned by plugin_management__install).",
            ja="プラグインのインストール名(plugin_management__install が返した名前)。",
        ),
    },
}
