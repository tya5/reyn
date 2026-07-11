"""Tool descriptions for the ``mcp`` category.

Every MCP-consumption / MCP-lifecycle ToolDefinition's description string
lives here as a reviewable ``ToolDescription`` record. Each ``.text``
value is byte-identical to its origin tool module's ``_X_DESCRIPTION``
constant, which now aliases to ``mcp.NAME.text`` so every call site is
unchanged.

Grouped here by feature (MCP), not by each entry's literal
``ToolDefinition.category=`` value: 11 entries carry ``category="discovery"``
in code (predating the ``mcp`` category) and 6 install/drop entries carry
``category="io"`` — but all 17 are MCP-specific verbs a reviewer wants
audited in one place. Excludes ``mcp_search_registry``, which lives in
``descriptions.discovery`` — its own ``ToolDefinition.category`` IS
``"discovery"`` and it is discovery-shaped, not install-shaped.

Covers, by origin module:
  ``mcp.py``: list_mcp_servers, list_mcp_tools, call_mcp_tool,
    describe_mcp_tool, list_mcp_resources, list_mcp_resource_templates,
    read_mcp_resource, subscribe_mcp_resource, unsubscribe_mcp_resource,
    list_mcp_prompts, get_mcp_prompt.
  ``mcp_verbs.py``: mcp_install_registry, mcp_install_package,
    mcp_install_local, mcp_call_tool.
  ``mcp_install.py``: mcp_install (phase-only legacy install op).
  ``mcp_drop.py``: mcp_drop_server.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ToolDescription

list_mcp_servers = ToolDescription(
    tool_name="list_mcp_servers",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — Type C closure",
    purpose="Enumerate configured MCP servers (name + description) for this agent.",
    text=(
        "List available MCP servers configured for this agent. "
        "Returns name + description per server."
    ),
    ja="このエージェント用に設定された MCP サーバーを列挙する。サーバーごとに name + description を返す。",
)

list_mcp_tools = ToolDescription(
    tool_name="list_mcp_tools",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — Type C closure",
    purpose=(
        "List the tools exposed by one MCP server, including inputSchema, "
        "so the LLM can construct call_mcp_tool args without an extra "
        "describe round-trip (#879)."
    ),
    text=(
        "List tools exposed by one MCP server "
        "(with description per tool)."
    ),
    ja="1つの MCP サーバーが公開するツールを一覧表示する（ツールごとに説明付き）。",
)

call_mcp_tool = ToolDescription(
    tool_name="call_mcp_tool",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Invoke a named tool on an MCP server with args matching its "
        "declared input schema."
    ),
    text=(
        "Invoke a mcp_tool on an MCP server. Construct args matching "
        "the mcp_tool's input schema (see describe_mcp_tool)."
    ),
    ja=(
        "MCP サーバー上の mcp_tool を呼び出す。mcp_tool の入力スキーマに"
        "合わせて引数を構築する（describe_mcp_tool 参照）。"
    ),
)

describe_mcp_tool = ToolDescription(
    tool_name="describe_mcp_tool",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — FP-0032 D4",
    purpose=(
        "Fetch one MCP tool's input schema when unsure how to construct "
        "call_mcp_tool's args."
    ),
    text=(
        "Get the input schema for one mcp_tool registered on an MCP server. "
        "Call this before call_mcp_tool if you're unsure how to "
        "construct the args."
    ),
    ja=(
        "MCP サーバーに登録された1つの mcp_tool の入力スキーマを取得する。"
        "call_mcp_tool の引数の組み立て方が分からない場合、事前に呼ぶ。"
    ),
)

list_mcp_resources = ToolDescription(
    tool_name="list_mcp_resources",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — #2597 slice ②a",
    purpose="Enumerate one MCP server's resources (uri + description per resource).",
    text=(
        "List resources exposed by one MCP server "
        "(with uri + description per resource)."
    ),
    ja="1つの MCP サーバーが公開するリソースを一覧表示する（uri + 説明付き）。",
)

list_mcp_resource_templates = ToolDescription(
    tool_name="list_mcp_resource_templates",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — #2597 slice ②a",
    purpose=(
        "Enumerate one MCP server's parameterized resource-URI templates, "
        "distinct from list_mcp_resources' concrete resource list."
    ),
    text=(
        "List resource templates (parameterized URI patterns) exposed by one "
        "MCP server. Use list_mcp_resources for concrete resources."
    ),
    ja=(
        "1つの MCP サーバーが公開するリソーステンプレート（パラメータ化"
        "された URI パターン）を一覧表示する。具体的なリソースには "
        "list_mcp_resources を使う。"
    ),
)

read_mcp_resource = ToolDescription(
    tool_name="read_mcp_resource",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — #2597 slice ②a",
    purpose=(
        "Read one MCP resource's content by URI, resolved from "
        "list_mcp_resources or a list_mcp_resource_templates template."
    ),
    text=(
        "Read the contents of one MCP resource by URI. Get the uri from "
        "list_mcp_resources (or by resolving a list_mcp_resource_templates "
        "template)."
    ),
    ja=(
        "URI で指定した1つの MCP リソースの内容を読む。uri は "
        "list_mcp_resources から取得する（または list_mcp_resource_"
        "templates のテンプレートを解決して得る）。"
    ),
)

subscribe_mcp_resource = ToolDescription(
    tool_name="subscribe_mcp_resource",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — #2597 slice ②b",
    purpose=(
        "Subscribe to server-pushed change notifications for one MCP "
        "resource; the notification itself carries no content — "
        "read_mcp_resource must be re-called to see it."
    ),
    text=(
        "Subscribe to server-pushed updates for one MCP resource by URI. When the "
        "server-side content changes, a mcp_resource_updated event is recorded — "
        "call read_mcp_resource again to see the new content (the push notification "
        "itself carries no content, just a signal that something changed)."
    ),
    ja=(
        "URI で指定した1つの MCP リソースについて、サーバー側からのプッシュ"
        "更新を購読する。サーバー側のコンテンツが変わると mcp_resource_"
        "updated イベントが記録される — 新しい内容を見るには read_mcp_"
        "resource を再度呼ぶ（プッシュ通知自体はコンテンツを持たず、変化の"
        "シグナルのみ）。"
    ),
)

unsubscribe_mcp_resource = ToolDescription(
    tool_name="unsubscribe_mcp_resource",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — #2597 slice ②b",
    purpose="Cancel a previously-established subscribe_mcp_resource subscription.",
    text=(
        "Unsubscribe from server-pushed updates for one MCP resource by URI "
        "(previously subscribed via subscribe_mcp_resource)."
    ),
    ja=(
        "URI で指定した1つの MCP リソースについて、以前 subscribe_mcp_"
        "resource で購読したサーバープッシュ更新を解除する。"
    ),
)

list_mcp_prompts = ToolDescription(
    tool_name="list_mcp_prompts",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — #2597 slice ②c",
    purpose="Enumerate one MCP server's prompts (name + description + arguments).",
    text=(
        "List prompts exposed by one MCP server "
        "(with name + description + arguments per prompt)."
    ),
    ja=(
        "1つの MCP サーバーが公開するプロンプトを一覧表示する（プロンプト"
        "ごとに name + description + arguments 付き）。"
    ),
)

get_mcp_prompt = ToolDescription(
    tool_name="get_mcp_prompt",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — #2597 slice ②c",
    purpose=(
        "Fetch one MCP prompt's rendered messages by name, using the "
        "argument schema from list_mcp_prompts."
    ),
    text=(
        "Fetch one rendered MCP prompt's messages by name. Get the name (and its "
        "argument schema) from list_mcp_prompts."
    ),
    ja=(
        "名前で指定した1つの MCP プロンプトのレンダリング済みメッセージを"
        "取得する。name（と引数スキーマ）は list_mcp_prompts から取得する。"
    ),
)

mcp_install_registry = ToolDescription(
    tool_name="mcp_install_registry",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — install source-axis split",
    purpose=(
        "Install an MCP server from the official registry by server_id "
        "(paired with mcp_search_registry candidates), handling the "
        "needs_secrets round-trip when secret env-vars are required."
    ),
    text=(
        "Install an MCP server from the official MCP registry by its "
        "registry name (server_id from mcp__search_registry candidates[].name). "
        "When the server requires secret environment variables that the "
        "operator has not yet set, the call returns status='needs_secrets' "
        "with a guide explaining the `reyn secret set <KEY>` command; relay "
        "that to the user and retry after they confirm secrets are set."
    ),
    ja=(
        "公式 MCP レジストリから server_id を指定してサーバーをインストール"
        "する（server_id は mcp__search_registry の candidates[].name から"
        "得る）。サーバーがシークレット環境変数を要求し未設定の場合、"
        "status='needs_secrets' と `reyn secret set <KEY>` の案内を返す — "
        "ユーザーに伝え、設定確認後に再試行する。"
    ),
)

mcp_install_package = ToolDescription(
    tool_name="mcp_install_package",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — install source-axis split",
    purpose=(
        "Install an MCP server from a third-party package channel "
        "(npm/pypi/docker) or a GitHub repo URL, for servers not in the "
        "official registry."
    ),
    text=(
        "Install an MCP server from a third-party package channel "
        "(npm / pypi / docker) or a GitHub repo URL. Use when the server "
        "isn't in the official registry (= mcp__search_registry returned "
        "no match). Secret detection works the same as install_registry "
        "for npm/pypi/docker; github URLs cannot pre-declare secrets."
    ),
    ja=(
        "サードパーティのパッケージチャネル（npm/pypi/docker）または "
        "GitHub リポジトリ URL から MCP サーバーをインストールする。公式"
        "レジストリにない場合（mcp__search_registry が一致なしを返した"
        "場合）に使う。npm/pypi/docker はシークレット検出が install_"
        "registry と同様に働くが、github URL は事前宣言できない。"
    ),
)

mcp_install_local = ToolDescription(
    tool_name="mcp_install_local",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — install source-axis split",
    purpose=(
        "Register a local MCP server ({command, args} pair) directly, for "
        "LLM-authored scripts or local dev servers, bypassing package "
        "registries."
    ),
    text=(
        "Install a local MCP server by registering a {command, args} pair "
        "directly. Use for LLM-authored scripts or local development "
        "servers. Bypasses package registries — cannot auto-detect required "
        "secrets, so pass env_overrides inline when the server needs env-vars."
    ),
    ja=(
        "{command, args} のペアを直接登録してローカル MCP サーバーを"
        "インストールする。LLM が作成したスクリプトやローカル開発サーバー"
        "向け。パッケージレジストリを経由しないため必要なシークレットを"
        "自動検出できない — env-var が必要な場合は env_overrides を"
        "インラインで渡す。"
    ),
)

mcp_call_tool = ToolDescription(
    tool_name="mcp_call_tool",
    surfaced=(
        "router + phase (gates.router=allow, gates.phase=allow) — generic "
        "fallback beneath per-tool universal-catalog actions"
    ),
    purpose=(
        "Generic fallback to call any installed MCP server's tool by "
        "<server>__<tool> identifier when no per-tool universal-catalog "
        "action is available."
    ),
    text=(
        "Call a tool on an installed MCP server — GENERIC FALLBACK. "
        "PREFER the per-tool 'mcp__<server>__<tool>' actions (e.g. "
        "'mcp__time__get_current_time') when one is listed: they take the target "
        "tool's own parameters directly (authoritative input_schema via "
        "describe_action), with no generic envelope. Use this generic verb only as "
        "a fallback when no per-tool action is available. Pass the tool identifier "
        "in <server>__<tool> form (e.g. 'time__get_current_time') as returned by "
        "mcp__list_tools, plus the tool's own args dict."
    ),
    ja=(
        "インストール済み MCP サーバーのツールを呼び出す（汎用フォール"
        "バック）。個別ツール専用の 'mcp__<server>__<tool>' アクションが"
        "リストにある場合はそちらを優先する（describe_action で権威ある"
        "入力スキーマを持つ、汎用エンベロープなし）。個別アクションが"
        "ない場合のみこの汎用verbを使う。ツール識別子は <server>__<tool> "
        "形式（mcp__list_tools が返す形）＋そのツール自身の引数dictを渡す。"
    ),
)

mcp_install = ToolDescription(
    tool_name="mcp_install",
    surfaced=(
        "phase-only (gates.router=deny, gates.phase=allow) — legacy Control "
        "IR install op, ADR-0026 + ADR-0029"
    ),
    purpose=(
        "Phase-authored install of an MCP server from the registry into a "
        "chosen scope config file, with permission gate + secret prompting "
        "owned by op_runtime.mcp_install."
    ),
    text=(
        "Install an MCP server from the registry. "
        "Fetches server.json, gates via permission resolver, "
        "prompts for secrets, and writes the server entry to the "
        "appropriate scope config file (local / project / user). "
        "Status: enabled — this tool's presence in your tool list means "
        "the required `file.write` and `http.get` permissions are verified. "
        "Call mcp_install directly; do not abort on permission concerns."
    ),
    ja=(
        "レジストリから MCP サーバーをインストールする（フェーズ専用の "
        "Control IR オペレーション）。server.json を取得し、パーミッション"
        "リゾルバでゲートし、シークレットを尋ね、適切なスコープの設定"
        "ファイル（local/project/user）にサーバーエントリを書き込む。"
        "このツールがツールリストに存在すること自体が file.write / "
        "http.get 権限確認済みを意味する — 権限懸念で中断せず直接呼ぶ。"
    ),
)

mcp_drop_server = ToolDescription(
    tool_name="mcp_drop_server",
    surfaced="router + phase (gates.router=allow, gates.phase=allow) — FP-0034 §D23",
    purpose=(
        "Remove a configured MCP server entry (the destructor counterpart "
        "to mcp_install), optionally clearing its secrets, gated by a "
        "distinct mcp_drop_server permission."
    ),
    text=(
        "Remove a configured MCP server. "
        "Counter-op to mcp_install — deletes the server entry from "
        "reyn.local.yaml / reyn.yaml / ~/.reyn/config.yaml (scope is "
        "auto-detected when omitted). Optionally cleans the matching "
        "${KEY} env entries from ~/.reyn/secrets.env. "
        "Permission-gated via mcp_drop_server (= distinct from "
        "mcp_install; install intent alone is insufficient)."
    ),
    ja=(
        "設定済みの MCP サーバーを削除する（mcp_install の対になる破壊的"
        "操作）。reyn.local.yaml / reyn.yaml / ~/.reyn/config.yaml から"
        "サーバーエントリを削除する（scope 省略時は自動検出）。任意で "
        "~/.reyn/secrets.env の対応する ${KEY} エントリも削除する。"
        "mcp_drop_server という別個のパーミッションでゲートされる"
        "（mcp_install とは別）。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "list_mcp_servers": list_mcp_servers,
    "list_mcp_tools": list_mcp_tools,
    "call_mcp_tool": call_mcp_tool,
    "describe_mcp_tool": describe_mcp_tool,
    "list_mcp_resources": list_mcp_resources,
    "list_mcp_resource_templates": list_mcp_resource_templates,
    "read_mcp_resource": read_mcp_resource,
    "subscribe_mcp_resource": subscribe_mcp_resource,
    "unsubscribe_mcp_resource": unsubscribe_mcp_resource,
    "list_mcp_prompts": list_mcp_prompts,
    "get_mcp_prompt": get_mcp_prompt,
    "mcp_install_registry": mcp_install_registry,
    "mcp_install_package": mcp_install_package,
    "mcp_install_local": mcp_install_local,
    "mcp_call_tool": mcp_call_tool,
    "mcp_install": mcp_install,
    "mcp_drop_server": mcp_drop_server,
}
