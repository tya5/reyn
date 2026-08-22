"""Tool descriptions for the ``discovery`` category.

Phase 1 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): every ``discovery``-category
ToolDefinition's description string lives here as a reviewable
``ToolDescription`` record. Each ``.text`` value is copied verbatim from
its origin tool module; the origin module now aliases its
``_X_DESCRIPTION`` module constant to ``discovery.NAME.text`` so every
call site is unchanged.

Covers: embed, web_fetch, web_search, mcp_search_registry,
universal_catalog's list_actions / search_actions / describe_action, and
FP-0066 P3c's search_knowledge (the ``knowledge`` category — semantic
search across the operator's own skill / memory / repo knowledge, distinct
from search_actions' tool-catalog search).

FP-0066 P1b: ``semantic_search`` (+ its ``_HIDE_LEGACY`` enriched variant)
and ``list_rag_sources`` — the agent-facing layer-1 in-core RAG tools
(ADR-0033 Phase 1 / #3026) — are RETIRED along with the ``ToolDescription``
records that used to live here. See
docs/deep-dives/proposals/0066-retrieval-two-groups-two-axes.md §9.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

embed = ToolDescription(
    tool_name="embed",
    surfaced="router (gates.router=allow) — both chat and pipeline",
    purpose=(
        "Raw embedding primitive so the caller can build their OWN "
        "external RAG store (composes with an external MCP vector-DB "
        "via pipeline; reyn hosts no user RAG store)."
    ),
    text=(
        "Embed a batch of texts into vectors using reyn's configured embedding "
        "model (raw primitive — no storage). Returns one vector per input text, "
        "in the same order. Use this to build your OWN persistent RAG store: "
        "embed your texts, then hand the vectors to your own vector-DB MCP "
        "tools (store/upsert) to persist them, and again at query time to embed "
        "a query before calling that store's search tool. reyn's own indexed "
        "sources / tool-use retrieval are OS-internal (not agent-callable); "
        "for personal memory retrieval use `list_memory` / `read_memory_body`."
    ),
    ja=(
        "テキストのバッチをベクトルに変換する生のプリミティブ（保存はしない）。"
        "ユーザー自身が外部の vector-DB MCP と組み合わせて自前の RAG ストアを"
        "構築するためのもの。reyn 自身が管理するインデックス済みソース / "
        "ツール利用検索は OS 内部専用（エージェントからは呼び出せない）。"
        "個人メモリの取得には list_memory / read_memory_body を使う。"
    ),
)

web_fetch = ToolDescription(
    tool_name="web_fetch",
    surfaced="router (gates.router=allow)",
    purpose=(
        "Fetch a single URL and return its text-extracted body, so the LLM can "
        "follow up a web_search result by actually reading the page."
    ),
    text=(
        "Fetch a single URL and return its text-extracted body. "
        "url: absolute http/https URL. "
        "Use after web_search to load a result page."
    ),
    ja=(
        "単一の URL を取得し、テキスト抽出した本文を返す。"
        "url: 絶対 http/https URL。"
        "web_search の結果ページを読み込む際に使う。"
    ),
)

web_search = ToolDescription(
    tool_name="web_search",
    surfaced="router (gates.router=allow)",
    purpose=(
        "Search the public web via DuckDuckGo when the user's question needs "
        "information outside reyn's indexed sources / memory."
    ),
    text=(
        "Search the public web with DuckDuckGo and return "
        "structured results. Standard search operators are "
        "supported in `query`: `site:<domain>` to scope to "
        "one site (e.g. `site:news.ycombinator.com`), "
        "`\"phrase\"` for exact match, `-term` to exclude. "
        "Use them when the user's intent is site-specific "
        "or phrase-anchored; plain keywords work otherwise. "
        "query: search string. "
        "max_results: cap on returned results (default 5)."
    ),
    ja=(
        "DuckDuckGo で公開ウェブを検索し、構造化された結果を返す。"
        "site:<domain> や \"完全一致\"、-除外語 といった標準の検索演算子"
        "を query 内で使える。ユーザーの意図がサイト限定・フレーズ一致の"
        "場合に使う。"
    ),
)

mcp_search_registry = ToolDescription(
    tool_name="mcp_search_registry",
    surfaced="router (gates.router=allow)",
    purpose=(
        "Search the official MCP registry for servers matching a "
        "natural-language capability request, feeding mcp_install_registry."
    ),
    text=(
        "Search the official MCP registry for servers matching a "
        "natural-language capability request. Returns candidates whose "
        "'name' field feeds mcp_install_registry. Multilingual — accepts "
        "queries in any language."
    ),
    ja=(
        "公式 MCP レジストリを自然言語の要求で検索し、該当するサーバー"
        "候補を返す。候補の 'name' フィールドは mcp_install_registry に"
        "渡して使う。多言語対応。"
    ),
)

list_actions = ToolDescription(
    tool_name="list_actions",
    surfaced="router (gates.router=allow) — universal catalog wrapper",
    purpose=(
        "Enumerate actions in the full catalog by category, so the LLM can "
        "discover category-listable capabilities before refusing a request."
    ),
    text=(
        "WHAT: Discover actions in the FULL catalog. "
        "Filter by category: `category=[...]` array (enum-restricted, exact "
        "category match). Omit or pass [] to enumerate everything visible. "
        "Returns {items: [{action_name, short_description}, ...], total: int}. "
        "An empty items array means no actions match — report this honestly. "
        "WHEN: PREFERRED FIRST for known-category enumeration (e.g. 'show me all "
        "memory_operation actions', 'what exec actions are available?') or exact-name "
        "lookup when you already know the category but not the exact entry. "
        "ALWAYS call list_actions BEFORE refusing a category-listable capability "
        "request. Refusing without a list_actions check is a FAILURE MODE "
        "(= the action you assumed missing often exists). "
        "For known-category enumeration pass `category=['exec']` to narrow. "
        "WHEN NOT: For semantic / natural-language / free-text discovery (e.g. "
        "'find an action that can ...', 'related to X', 'something for X' — the "
        "request may be phrased in any language, including Japanese and other "
        "non-English input) use search_actions instead — it returns relevance-ranked matches across "
        "categories rather than a flat enumeration. If you already know the "
        "exact action name, skip both and call invoke_action directly. "
        "PREFERRED OVER: Guessing action names + refusing capability requests — "
        "list_actions returns the canonical qualified names (e.g. "
        "mcp_call_tool, delegate_to_agent) that invoke_action and "
        "describe_action expect. "
        "POST_CALL: After list_actions reveals at least one matching action, you "
        "MUST follow with describe_action or invoke_action. Do NOT reply directly "
        "— silent stop after enumeration is a failure mode. When items is empty, "
        "honestly tell the user no matching actions are available."
    ),
    ja=(
        "フルカタログからカテゴリ指定でアクションを列挙する。既知カテゴ"
        "リの列挙や、カテゴリは分かるが正確なエントリ名が分からない場合"
        "に最初に呼ぶべきツール。自由文/意味的検索には search_actions を"
        "使う。"
    ),
)

search_actions = ToolDescription(
    tool_name="search_actions",
    surfaced="router (gates.router=allow) — universal catalog wrapper",
    purpose=(
        "Semantic, multilingual search across available actions, for "
        "free-text / natural-language capability requests that don't name a "
        "specific category."
    ),
    text=(
        "WHAT: Semantic search across available actions — multilingual, "
        "embedding-based, relevance-ranked. "
        "Returns {items: [{action_name, short_description, score}, ...]}. "
        "WHEN: PREFERRED FIRST for semantic / natural-language / free-text "
        "queries — when the user asks to find / search for / something related "
        "to / similar to / something for X / actions about Y / find ... related "
        "to Z (the request may be phrased in any language, including Japanese "
        "and other non-English input), or describes "
        "an intent without naming a specific category. ALWAYS call search_actions "
        "BEFORE refusing a semantic-intent capability request. Refusing without "
        "a search_actions check is a FAILURE MODE (= relevance ranking may surface "
        "the action across categories that a flat enumeration would miss). "
        "Multilingual — works in any language (Japanese, English, etc.). "
        "Handles both semantic descriptions AND free-text keyword lookup "
        "(e.g. an action containing 'http'). "
        "WHEN NOT: For known-category enumeration (e.g. 'show me all exec actions', "
        "'list of memory_operation actions' — again, phrasable in any language) "
        "use list_actions(category=[...]) instead — "
        "it returns the flat catalogue slice rather than relevance-ranked hits. "
        "If you already know the exact action name, skip both and call "
        "invoke_action directly. "
        "Available only when embedding is enabled (reyn.yaml "
        "embedding.enabled: true). "
        "POST_CALL: After search_actions reveals at least one matching action, "
        "you MUST follow with describe_action or invoke_action. Do NOT reply "
        "directly — silent stop after semantic search is a failure mode."
    ),
    ja=(
        "利用可能なアクション群に対する多言語の意味的検索（埋め込みベース、"
        "関連度ランキング付き）。自由文・自然言語のリクエストで、特定の"
        "カテゴリ名を指定していない場合に最初に呼ぶべきツール。既知カテゴ"
        "リの列挙には list_actions を使う。"
    ),
)

describe_action = ToolDescription(
    tool_name="describe_action",
    surfaced="router (gates.router=allow) — universal catalog wrapper",
    purpose=(
        "Fetch the full description, input schema, and metadata for one "
        "action, so the LLM knows the exact argument shape before "
        "invoke_action."
    ),
    text=(
        "WHAT: Get the full description, input schema, and metadata for one action "
        "or resource. Returns {description, input_schema, metadata}. "
        "WHEN: Use this before invoke_action when you need to know the exact "
        "argument shape of an action. Should be called whenever you have the "
        "action_name but are unsure of the required args. "
        "WHEN NOT: If you already know the input schema (from a previous call or "
        "the action takes no args), skip this and call invoke_action directly. "
        "PREFERRED OVER: Guessing argument names — describe_action returns the "
        "authoritative input_schema. On unknown action_name, returns an error "
        "with similar-name suggestions. "
        "POST_CALL: After describe_action, you MUST follow with invoke_action or "
        "explain in text why not. Never stop silently after investigation."
    ),
    ja=(
        "1つのアクション/リソースについて、完全な説明・入力スキーマ・"
        "メタデータを取得する。invoke_action の前に正確な引数の形が"
        "分からない場合に使う。引数名を推測する代わりに使うべきツール。"
    ),
)

search_knowledge = ToolDescription(
    tool_name="search_knowledge",
    surfaced=(
        "router (gates.router=allow) — new "
        "``knowledge`` category (FP-0066 P3c, #3247 firm §3/§5); visible "
        "only when ``embedding.enabled: true`` (shares the search_actions "
        "D14 visibility predicate — see ``is_search_available``)"
    ),
    purpose=(
        "Semantic search across the operator's own knowledge — skills, "
        "memory entries, and repo docs/source — one entity-level row per "
        "hit, so the LLM can discover relevant material before naming an "
        "exact activation path."
    ),
    text=(
        "WHAT: Semantic search across the operator's OWN knowledge — every "
        "installed skill's body, every memory entry, and the reyn repo's "
        "docs and source — merged into one relevance-ranked result set. "
        "Returns {items: [{kind, id, title, description}, ...]}, one row "
        "PER ENTITY (kind ∈ 'skill' | 'memory' | 'repo_doc' | 'repo_src'; "
        "id is kind-native — a skill name, a memory doc path, or a repo "
        "file path — never an abstract handle). "
        "WHEN: Use this to find WHAT knowledge exists before you know its "
        "exact name or path — a free-text description of what you're "
        "looking for (may be phrased in any language). "
        "WHEN NOT: If you already know the exact skill name / memory path / "
        "repo path, skip search and activate it directly. Do not use this "
        "for tool/action discovery — that is search_actions, a separate "
        "index. "
        "ACTIVATION IS KIND-ROUTED, not unified: after a hit, activate it "
        "via the verb matching its kind — skill -> load_skill(name=id), "
        "memory -> read_memory_body(layer, slug) derived from id, repo_doc/"
        "repo_src -> reyn_repo_read(path=id). There is no single generic "
        "'load' call across kinds."
    ),
    ja=(
        "運用者自身のナレッジ（インストール済みスキル本文・メモリ・"
        "reyn リポジトリのdocs/ソース）を横断する意味的検索。エンティティ"
        "単位で1行を返す（kind ごとに id はネイティブ — スキル名・メモリ"
        "パス・リポジトリのファイルパス。抽象ハンドルではない）。活性化は"
        "kind ごとに異なるツールへルーティングする（統一 load 動詞は"
        "存在しない）。search_actions（ツールカタログ検索）とは別の"
        "インデックス。"
    ),
)

describe_session = ToolDescription(
    tool_name="describe_session",
    surfaced="router (gates.router=allow)",
    purpose=(
        "Let the model check its own write scope / repo position / auth "
        "status instead of guessing or asking the user (#5012-A)."
    ),
    text=(
        "Report this session's own position: (1) write scope as DECLARED by "
        "the sandbox policy (never a resolved/effective scope — a permission "
        "check may still deny a specific path), (2) repo path, git branch/HEAD, "
        "venv path, and ruff/pytest/mkdocs availability, and (3) auth status "
        "for reyn's own OAuth-managed providers only (authenticated: true/false "
        "+ a reason — never a token or scope; a third-party CLI's own auth, "
        "e.g. `gh auth status`, is out of scope — run that CLI directly for "
        "its own auth state). No arguments."
    ),
    ja=(
        "このセッション自身の位置情報を報告する: (1) sandbox policy が宣言する"
        "書き込みスコープ（解決済みスコープではない — 個別パスの許可判定は別）、"
        "(2) リポジトリパス・gitブランチ/HEAD・venvパス・ruff/pytest/mkdocsの"
        "有無、(3) reyn自身がOAuth管理するプロバイダの認証状態のみ（token/"
        "scopeは含まない。サードパーティCLI自身の認証は対象外）。引数なし。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "embed": embed,
    "web_fetch": web_fetch,
    "web_search": web_search,
    "mcp_search_registry": mcp_search_registry,
    "list_actions": list_actions,
    "search_actions": search_actions,
    "search_knowledge": search_knowledge,
    "describe_action": describe_action,
    "describe_session": describe_session,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────
#
# web_fetch / web_search have no param-level descriptions in their origin
# schemas (url/query are bare-typed) — no entries needed here.

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "embed": {
        "texts": ParamDescription(
            text="Texts to embed. Returned vectors preserve this order.",
            ja="埋め込み対象のテキスト群。返るベクトルはこの順序を保持する。",
        ),
        "embedding_model": ParamDescription(
            text=(
                "Embedding model class (light/standard/strong) or a full "
                "provider model id."
            ),
            ja="埋め込みモデルクラス（light/standard/strong）またはプロバイダのフルモデルID。",
        ),
    },
    "mcp_search_registry": {
        "text": ParamDescription(
            text=(
                "Natural-language capability request (e.g. \"github "
                "related\", \"image generation\", \"PDF handling\") — "
                "the query may be in any language, including Japanese "
                "and other non-English input."
            ),
            ja=(
                "自然言語での能力リクエスト（例「github関連」「画像生成」"
                "「PDF処理」） — 日本語を含むどの言語でもよい。"
            ),
        ),
    },
    "list_actions": {
        # NOTE: the origin schema appends ``", ".join(CATEGORIES) + "."`` to
        # this text at import time (the live category list, not a literal) —
        # ``.text`` here is the STATIC prefix only; the origin module still
        # does the concatenation so the byte-identical rendered string is
        # unchanged.
        "category": ParamDescription(
            text=(
                "Filter by category. Pass an array of category names "
                "(e.g. category=['exec'], category=['web', 'file']). "
                "Omit or pass [] to include all categories. "
                "Categories: "
            ),
            ja=(
                "カテゴリで絞り込む。カテゴリ名の配列を渡す（例 "
                "category=['exec']）。省略または [] で全カテゴリを含む。"
                "（末尾に実際のカテゴリ一覧が動的に付加される）"
            ),
        ),
        "offset": ParamDescription(
            text="Pagination offset (default 0).",
            ja="ページングオフセット（デフォルト 0）。",
        ),
        "limit": ParamDescription(
            text="Page size (default 10).",
            ja="1ページのサイズ（デフォルト 10）。",
        ),
    },
    "search_actions": {
        "query": ParamDescription(
            text="Natural-language query in any language.",
            ja="任意の言語での自然言語クエリ。",
        ),
        "category": ParamDescription(
            text="Optional category restriction.",
            ja="任意のカテゴリ制限。",
        ),
        "limit": ParamDescription(
            text="Top-K results to return (default 10).",
            ja="返す上位K件の結果数（デフォルト 10）。",
        ),
    },
    "search_knowledge": {
        "query": ParamDescription(
            text=(
                "Natural-language query in any language — describe what "
                "knowledge you're looking for (skill / memory / repo doc "
                "or source content)."
            ),
            ja="任意の言語での自然言語クエリ — 探しているナレッジの内容を記述する。",
        ),
        "limit": ParamDescription(
            text="Top-K entities to return (default 10), after chunk->entity aggregation.",
            ja="返す上位K件のエンティティ数（デフォルト 10、chunk→entity 集約後）。",
        ),
    },
    "describe_action": {
        "action_name": ParamDescription(
            text=(
                "Qualified name of the action to describe "
                "(e.g. 'mcp_call_tool', 'read_memory_body')."
            ),
            ja=(
                "説明対象のアクションの修飾名（例 "
                "'mcp_call_tool', 'read_memory_body'）。"
            ),
        ),
    },
}
