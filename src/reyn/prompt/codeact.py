"""§C — CodeAct's static code-API instructional header.

Feeds ``reyn.tools.schemes.codeact._render_code_api``. That function mixes a
static instructional header (this module) with a per-catalog-entry loop that
renders LIVE catalog data (function signatures from the currently installed
tool catalog) — the loop is NOT extractable (it varies with the installed
catalog, it is not fixed text), so it stays in ``codeact.py`` (the scheme
module) as assembly logic. Only the static header lines move here.
"""
from __future__ import annotations

# WHEN: always — the sole tool-use instruction CodeAct's scheme presents (it
#       REPLACES the universal invoke_action/list_actions SP region entirely).
# WHERE: reyn.tools.schemes.codeact._render_code_api, prepended before the
#        per-entry function-signature lines ("Available functions:" onward).
# WHY: #1658 — direct function-call code-API (not the old `tool('name', ...)`
#      string-proxy) so the action name can never be a hallucinated produced
#      string; carries the whole CodeAct contract (act = one fenced python
#      block, prose = terminal final answer).
# 日本語訳: CodeAct scheme が提示する唯一のツール利用指示。関数を直接名前で
#      呼ぶ code-API 形式（文字列プロキシではない）で、アクション名の捏造を
#      構造的に防ぐ。単一の fenced python block = 実行、prose = 最終回答。
CODEACT_STATIC_HEADER: list[str] = [
    "## Tool use — this agent acts by running Python, not JSON tool calls",
    "",
    "To DO anything (read a file, call an action, compute), respond with a "
    "SINGLE fenced ```python block and NOTHING else — no prose before or after it, "
    "no \"I am a Reyn agent\" preamble on an action turn. Inside the block, call the "
    "available functions DIRECTLY by name and assign your final value to `result`:",
    "",
    "    result = file__read(path=\"README.md\")",
    "",
    "Each function returns the action's result, or raises if the action is denied / "
    "excluded / unknown. The Python standard library is available; filesystem / "
    "network / subprocess are sandboxed — reach the outside world ONLY by calling "
    "these functions.",
    "",
    "When you are DONE (answer in hand, no more actions to run): reply in plain "
    "prose with NO code block — that ends the turn. A turn is EITHER one fenced "
    "```python block OR a plain-prose final answer, never both.",
    "",
    "Available functions:",
]
