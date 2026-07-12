"""Tool descriptions for the ``context`` category.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): ``compact`` (#272 / #1128), the
voluntary history-compaction tool. ``.text`` is copied verbatim from
``tools/compact.py``; the origin module now aliases ``_COMPACT_DESCRIPTION``
to ``context.compact.text``.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

compact = ToolDescription(
    tool_name="compact",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Let the model voluntarily free context-window room ahead of the "
        "mandatory retry_loop backstop compaction, when it still has work "
        "to do and the window is filling."
    ),
    text=(
        "Compact the conversation history now: summarise older turns to free up "
        "context window. Use this when the 'Context window' status shows the free "
        "window getting low and you still have work to do — compacting first frees "
        "room so subsequent steps and large tool results fit. Returns the freed "
        "tokens and the free window afterwards (exact tokens). The system also "
        "compacts automatically as a backstop; this lets you do it proactively."
    ),
    ja=(
        "会話履歴を今すぐ圧縮する: 古いターンを要約してコンテキストウィ"
        "ンドウを空ける。'Context window' の空き表示が少なくなり、まだ"
        "作業が残っている場合に使う。解放されたトークン数とその後の空き"
        "ウィンドウ（正確なトークン数）を返す。システムはバックストップ"
        "として自動でも圧縮するが、これを使えば能動的に行える。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "compact": compact,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "compact": {
        "reason": ParamDescription(
            text=(
                "Optional short rationale for the audit trail (e.g. 'window "
                "low before reading large file'). Not interpreted by the OS."
            ),
            ja=(
                "監査証跡向けの任意の短い理由（例：'大きなファイルを読む前に"
                "空きが少ない'）。OS はこの値を解釈しない。"
            ),
        ),
    },
}
