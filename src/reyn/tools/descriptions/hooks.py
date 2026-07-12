"""Tool descriptions for the ``hooks`` category.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): ``hooks_add`` (#2073 S3), the
agent-self-reload trigger that writes to the runtime hooks layer
(``.reyn/config/hooks.yaml``). ``.text`` is copied verbatim from
``tools/hooks.py``; the origin module now aliases
``_HOOKS_ADD_DESCRIPTION`` to ``hooks.hooks_add.text``.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

hooks_add = ToolDescription(
    tool_name="hooks_add",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Let the agent expand its own hooks (self-directed continuation or "
        "recurring injected context), bounded by the write-gate + "
        "validate-before-apply + permission safety trifecta."
    ),
    text=(
        "Add a push hook at an agent-lifecycle point (e.g. a turn_end self-continuation, "
        "or a context-inject). The hook is written to your runtime hooks layer "
        "(.reyn/config/hooks.yaml) and applied at the next turn boundary — it joins your existing "
        "hooks additively. Use for self-directed continuation or recurring injected "
        "context. Cannot touch startup config (reyn.yaml is restart-only)."
    ),
    ja=(
        "エージェントのライフサイクルポイント（turn_end の自己継続や"
        "コンテキスト注入など）にプッシュフックを追加する。フックは"
        "ランタイムの hooks レイヤー（.reyn/config/hooks.yaml）に書き込まれ、"
        "次のターン境界で適用される（既存フックに追加される）。自己主導"
        "の継続や定期的なコンテキスト注入に使う。起動時設定"
        "（reyn.yaml、再起動時のみ反映）には触れられない。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "hooks_add": hooks_add,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "hooks_add": {
        "on": ParamDescription(
            text="The lifecycle point the hook fires at.",
            ja="フックが発火するライフサイクルポイント。",
        ),
        "message": ParamDescription(
            text="The message pushed when the hook fires (a Jinja2 template is allowed).",
            ja="フック発火時にプッシュされるメッセージ（Jinja2 テンプレート可）。",
        ),
        "wake": ParamDescription(
            text=(
                "true → the push starts a new turn (self-continuation, capability E); "
                "false → it rides along with the next turn as context (capability C). "
                "Default true."
            ),
            ja=(
                "true なら新しいターンを開始（自己継続、capability E）、false なら"
                "次のターンにコンテキストとして相乗り（capability C）。デフォルト true。"
            ),
        ),
        "push_when": ParamDescription(
            text="Optional Jinja2 → bool; when it renders false the push is skipped. Default 'true'.",
            ja="任意の Jinja2 → bool 条件。false になるとプッシュはスキップされる。デフォルト 'true'。",
        ),
        "name": ParamDescription(
            text="Optional label surfaced as the [hook:<name>] attribution prefix.",
            ja="[hook:<name>] という帰属プレフィックスとして表示される任意ラベル。",
        ),
    },
}
