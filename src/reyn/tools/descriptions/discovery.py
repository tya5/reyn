"""Tool descriptions for the ``discovery`` category.

Phase 1 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): every ``discovery``-category
ToolDefinition's description string lives here as a reviewable
``ToolDescription`` record. Each ``.text`` value is copied verbatim from
its origin tool module; the origin module now aliases its
``_X_DESCRIPTION`` module constant to ``discovery.NAME.text`` so every
call site is unchanged.

Covers: embed, semantic_search (+ the currently-unwired
``_HIDE_LEGACY`` enriched variant), list_rag_sources, web_fetch,
web_search, mcp_search_registry, and universal_catalog's list_actions /
search_actions / describe_action.

``list_rag_sources`` (#3026) is NOT a relocation — the tool is new. It is
the discovery verb that replaced the ``rag_corpus__<name>`` catalog
category when #3026 collapsed the per-resource action surface, so
``semantic_search``'s ``sources`` argument stays answerable without one
action per corpus in ``tools=``. Its text (and ``semantic_search``'s, which
used to point at an "Indexed sources" system-prompt section that was never
rendered in the wrapper-only path — the vestigial ``indexed_sources_section``
parameter and its per-turn prefetch were removed in #3025) names it
explicitly, because a required
closed-set argument with no stated way to enumerate it is the same
reachability gap #2971 closed for skills.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

embed = ToolDescription(
    tool_name="embed",
    surfaced="router (gates.router=allow, gates.phase=allow) — both chat and pipeline",
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
        "a query before calling that store's search tool. For reyn's OWN "
        "indexed sources / memory / tool-use retrieval, use `semantic_search` "
        "instead — "
        "it embeds and searches in one call over reyn-managed indexes."
    ),
    ja=(
        "テキストのバッチをベクトルに変換する生のプリミティブ（保存はしない）。"
        "ユーザー自身が外部の vector-DB MCP と組み合わせて自前の RAG ストアを"
        "構築するためのもの。reyn 自身が管理するインデックス済みソース / "
        "メモリ / ツール利用検索には embed でなく semantic_search を使う。"
    ),
)

semantic_search = ToolDescription(
    tool_name="semantic_search",
    surfaced=(
        "router + phase (gates.router=allow, gates.phase=allow) — the primary "
        "LLM entry point for semantic search over indexed sources"
    ),
    purpose=(
        "Search reyn-managed indexed sources by natural-language query; the "
        "primary retrieval tool for 'what is X?' / topic-lookup style "
        "questions when an indexed source covers the topic."
    ),
    text=(
        "Search indexed sources by natural-language query. Returns top-K "
        "relevant chunks with text + metadata. Use this when the user's "
        "question is about a topic an indexed source covers — including "
        "'what is X?', 'explain X', 'how does X work?' style questions. "
        "Call `list_rag_sources` to see which sources exist; each source's "
        "description tells you what topics it covers. "
        "Prefer this over `reyn_repo_read` / file_read when an indexed source "
        "description matches the question's topic — semantic search across "
        "indexed chunks is more reliable than guessing a file path."
    ),
    ja=(
        "自然言語クエリでインデックス済みソースを検索する。トップKの関連"
        "チャンク（テキスト＋メタデータ）を返す。ユーザーの質問がインデック"
        "ス済みソースの話題と一致する場合（「Xとは？」「Xを説明して」等）に"
        "使う。ファイルパスを推測するより、list_rag_sources で得たソース"
        "からこのツールを使う方が信頼できる。"
    ),
)

# B23-PRE-1 SP role-separation: enriched WHAT/WHEN/WHEN_NOT variant carrying
# the semantic_search vs memory disambiguation that previously lived in the
# SP disambiguation block. Not currently wired into any ToolDefinition (a
# bare constant a wrapper-only describe_action path may expose); kept
# alongside `semantic_search` above (which stays byte-identical to the
# pre-rename `_RECALL_DESCRIPTION` for LLMReplay fixture stability).
semantic_search_hide_legacy = ToolDescription(
    tool_name="semantic_search",
    surfaced=(
        "NOT currently wired into any ToolDefinition — a bare constant a "
        "wrapper-only describe_action path may expose (B23-PRE-1)"
    ),
    purpose=(
        "Enriched WHAT/WHEN/WHEN_NOT variant disambiguating semantic_search "
        "from memory retrieval, for a wrapper-only exposure path."
    ),
    text=(
        "WHAT: Semantic search over indexed corpora (= RAG retrieval). "
        "Returns top-K relevant chunks with text + metadata. "
        "WHEN: Use when user asks 'search', 'find in docs', 'lookup', or any "
        "'what is X?' / 'explain X' / 'how does X work?' style question when "
        "an indexed source covers the topic. Multilingual — works across languages. "
        "WHEN NOT: "
        "For personal memory retrieval, use `list_memory` / `read_memory_body`. "
        "semantic_search is for indexed corpora, NOT memory. "
        "The word 'recall' in user input refers to THIS tool — never map it "
        "to the memory tools. "
        "PREFERRED OVER: the memory tools when content is indexed (source-"
        "level), not personal memory. "
        "Call `list_rag_sources` to see which sources exist; "
        "each source's description tells you what topics it covers. "
        "Prefer this over reyn_repo_read / file_read when an indexed source "
        "description matches the question's topic — semantic search across "
        "indexed chunks is more reliable than guessing a file path."
    ),
    ja=(
        "semantic_search とメモリ検索を区別するための、"
        "WHAT/WHEN/WHEN NOT 形式の拡張版説明。個人メモリの取得には "
        "read_memory_body を使い、インデックス済みコーパスの検索には "
        "semantic_search を使う、という判断基準を明示する。現在どの "
        "ToolDefinition にも配線されていない補助定数。"
    ),
)

list_rag_sources = ToolDescription(
    tool_name="list_rag_sources",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "The discovery half of the RAG surface (#3026): names the indexed "
        "corpora so `semantic_search`'s `sources` argument is answerable. "
        "Replaces the per-corpus `rag_corpus__<name>` catalog actions, whose "
        "count scaled with the operator's corpora."
    ),
    text=(
        "List the indexed sources (RAG corpora) available in this session, "
        "with each source's name, description, and indexed chunk count. Call "
        "this before `semantic_search` when you do not already know a source "
        "name: `semantic_search` requires `sources`, and the names are chosen "
        "by the operator, so they cannot be guessed. An empty list means "
        "nothing has been indexed yet — say so rather than guessing a source "
        "name."
    ),
    ja=(
        "このセッションでインデックス済みのソース（RAG コーパス）を、"
        "名前・説明・チャンク数つきで列挙する。semantic_search の sources は "
        "必須かつ運用者が決めた名前なので推測できない。ソース名を知らない"
        "場合はまずこれを呼ぶ。#3026 で rag_corpus__<name> のコーパス毎の"
        "アクション（= payload が運用者のコーパス数に比例して増える原因）を"
        "畳んだ代わりに置かれた、定数個の discovery verb。"
    ),
)

web_fetch = ToolDescription(
    tool_name="web_fetch",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Fetch a single URL and return a structured preview + a path_ref to "
        "the full body, so the LLM can follow up a web_search result without "
        "inlining the whole page into context."
    ),
    text=(
        "Fetch a single URL. Returns a structured preview "
        "(title, outline, first paragraph, link count for HTML; "
        "first lines for text) plus a path_ref to the full body "
        "stored under .reyn/tool-results/. url: absolute http/https URL. "
        "max_length: cap on extracted body length (default 50000). "
        "Use after web_search to load a result page; call "
        "file__read(path) to read the full body."
    ),
    ja=(
        "単一の URL を取得する。構造化されたプレビュー（タイトル、アウト"
        "ライン、冒頭段落、リンク数など）と、本文全体への path_ref を返す。"
        "web_search の結果ページを読み込む際に使う。本文全体は "
        "file__read(path) で読む。"
    ),
)

web_search = ToolDescription(
    tool_name="web_search",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
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
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Search the official MCP registry for servers matching a "
        "natural-language capability request, feeding mcp_install_registry."
    ),
    text=(
        "Search the official MCP registry for servers matching a "
        "natural-language capability request. Returns candidates whose "
        "'name' field feeds mcp__install_registry. Multilingual — accepts "
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
    surfaced="router-only (gates.router=allow, gates.phase=deny) — universal catalog wrapper",
    purpose=(
        "Enumerate actions in the full catalog by category (a superset of "
        "the hot-list functions), so the LLM can discover category-listable "
        "capabilities before refusing a request."
    ),
    text=(
        "WHAT: Discover actions in the FULL catalog (= a superset of the hot-list "
        "function entries you can see directly). The hot-list shown in your "
        "function list is a curated subset; this tool reveals the rest. "
        "Filter by category: `category=[...]` array (enum-restricted, exact "
        "category match). Omit or pass [] to enumerate everything visible. "
        "Returns {items: [{qualified_name, short_description}, ...], total: int}. "
        "An empty items array means no actions match — report this honestly. "
        "WHEN: PREFERRED FIRST for known-category enumeration (e.g. 'show me all "
        "memory_operation actions', 'what exec actions are available?') or exact-name "
        "lookup when you already know the category but not the exact entry. "
        "ALWAYS call list_actions BEFORE refusing a category-listable capability "
        "request. Refusing without a list_actions check is a FAILURE MODE "
        "(= the action you assumed missing may exist behind the hot-list). "
        "For known-category enumeration pass `category=['exec']` to narrow. "
        "WHEN NOT: For semantic / natural-language / free-text discovery (e.g. "
        "'find an action that can ...', 'related to X', 'something for X' — the "
        "request may be phrased in any language, including Japanese and other "
        "non-English input) use search_actions instead — it returns relevance-ranked matches across "
        "categories rather than a flat enumeration. If you already know the "
        "exact action name, skip both and call invoke_action directly. "
        "PREFERRED OVER: Guessing action names + refusing capability requests — "
        "list_actions returns the canonical qualified names (e.g. "
        "mcp__call_tool, multi_agent__delegate) that invoke_action and "
        "describe_action expect. "
        "POST_CALL: After list_actions reveals at least one matching action, you "
        "MUST follow with describe_action or invoke_action. Do NOT reply directly "
        "— silent stop after enumeration is a failure mode. When items is empty, "
        "honestly tell the user no matching actions are available."
    ),
    ja=(
        "フルカタログ（ホットリストの上位互換）からカテゴリ指定でアクショ"
        "ンを列挙する。既知カテゴリの列挙や、カテゴリは分かるが正確な"
        "エントリ名が分からない場合に最初に呼ぶべきツール。自由文/意味的"
        "検索には search_actions を使う。"
    ),
)

search_actions = ToolDescription(
    tool_name="search_actions",
    surfaced="router-only (gates.router=allow, gates.phase=deny) — universal catalog wrapper",
    purpose=(
        "Semantic, multilingual search across available actions, for "
        "free-text / natural-language capability requests that don't name a "
        "specific category."
    ),
    text=(
        "WHAT: Semantic search across available actions — multilingual, "
        "embedding-based, relevance-ranked. "
        "Returns {items: [{qualified_name, short_description, score}, ...]}. "
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
        "Available only when an embedding class is configured (reyn.yaml "
        "action_retrieval.embedding_class). "
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
    surfaced="router-only (gates.router=allow, gates.phase=deny) — universal catalog wrapper",
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
        "qualified_name but are unsure of the required args. "
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

ALL: dict[str, ToolDescription] = {
    "embed": embed,
    "semantic_search": semantic_search,
    "semantic_search_hide_legacy": semantic_search_hide_legacy,
    "web_fetch": web_fetch,
    "web_search": web_search,
    "mcp_search_registry": mcp_search_registry,
    "list_actions": list_actions,
    "search_actions": search_actions,
    "describe_action": describe_action,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────
#
# web_fetch / web_search have no param-level descriptions in their origin
# schemas (url/max_length/query are bare-typed) — no entries needed here.

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
    "semantic_search": {
        "query": ParamDescription(
            text="Natural language query to search for.",
            ja="検索する自然言語クエリ。",
        ),
        "sources": ParamDescription(
            text="Logical source names to search (from list_rag_sources).",
            ja="検索対象の論理ソース名（list_rag_sources の結果から）。",
        ),
        "top_k": ParamDescription(
            text="Number of top chunks to return.",
            ja="返す上位チャンク数。",
        ),
        "filters": ParamDescription(
            text="ChunkMetadata field equality filters (e.g. source_path).",
            ja="ChunkMetadata フィールドの等価フィルタ（例 source_path）。",
        ),
        "embedding_model": ParamDescription(
            text=(
                "Fallback embedding model class (light/standard/strong) or "
                "full model id, used ONLY for a source with no recorded "
                "model yet — every already-indexed source auto-adopts its "
                "OWN recorded model regardless of this value (multi-model "
                "correctness, FP-0057 Phase 2a)."
            ),
            ja=(
                "フォールバックの埋め込みモデルクラス（light/standard/strong）"
                "またはフルモデルID。まだ記録済みモデルがないソースにのみ使わ"
                "れる — 既にインデックス済みのソースはこの値に関係なく自身の"
                "記録済みモデルを自動採用する（マルチモデル正当性、FP-0057 "
                "Phase 2a）。"
            ),
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
    "describe_action": {
        "action_name": ParamDescription(
            text=(
                "Qualified name of the action to describe "
                "(e.g. 'mcp__call_tool', 'rag_operation__semantic_search')."
            ),
            ja=(
                "説明対象のアクションの修飾名（例 "
                "'mcp__call_tool', 'rag_operation__semantic_search'）。"
            ),
        ),
    },
}
