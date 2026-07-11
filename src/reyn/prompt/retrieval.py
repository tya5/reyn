"""§D — retrieval's search-guidance SP (the two ``_search_sp`` variants).

Feeds ``reyn.tools.schemes.retrieval._search_sp``, used only by
``RetrievalScheme`` and injected into ``slot_post_catalog``. Retrieval runs
with ``universal_wrappers_enabled=False`` — the OS's named-gate "## Action
categories" block is off — so without this fragment the LLM would see the
``search_actions`` tool with no usage guidance.
"""
from __future__ import annotations

# WHEN: RetrievalScheme's non-terminal presentation (the search tool is still
#       being offered, RePresent convergence not yet reached).
# WHERE: injected at slot_post_catalog.
# WHY: teaches the search-first idiom for the namespace/retrieval paradigm —
#      the LLM is not shown the full tool catalog, so it must search before
#      it can act.
# 日本語訳: 検索が未収束（RePresent が続いている）間に描画される、
#      「まず search_actions で検索してから呼ぶ」という手順の説明。
SEARCH_SP_NON_TERMINAL = (
    "## Finding tools\n"
    "You are not shown the full tool catalog up front. To act, first call "
    "`search_actions(query=...)` with a natural-language description of what "
    "you need; the matching tools are then presented for you to call "
    "directly. Search before you act, and refine the query if the first "
    "matches do not fit."
)

# WHEN: RetrievalScheme's terminal presentation (convergence reached, the
#       search tool has been dropped from the presented tools).
# WHERE: injected at slot_post_catalog.
# WHY: flips the instruction from "search first" to "call one of the
#      presented matches" once the matched tools are already on the table.
# 日本語訳: 収束済み（検索ツールが外され、候補が提示済み）のときに描画される、
#      「提示された候補から直接呼ぶ」という指示。
SEARCH_SP_TERMINAL = (
    "## Finding tools\n"
    "The tools matching your search are now available above. Call the "
    "one that fits the request directly."
)
