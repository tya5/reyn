"""Tool descriptions for the ``hooks`` category.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): ``hooks_add`` (#2073 S3), the
agent-self-reload trigger that writes to the runtime hooks layer
(``.reyn/config/hooks.yaml``). ``.text`` is copied verbatim from
``tools/hooks.py``; the origin module now aliases
``_HOOKS_ADD_DESCRIPTION`` to ``hooks.hooks_add.text``.

``emit_hook_event`` (Hook-Event Redesign Phase 5 part 2, proposal
``docs/deep-dives/proposals/0059-hook-event-redesign.md`` §8): the
``emit_hook_event`` Control-IR op's LLM-facing description, aliased onto
``tools/emit_hook_event.py``'s ``_EMIT_HOOK_EVENT_DESCRIPTION`` the same way.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

hooks_add = ToolDescription(
    tool_name="hooks_add",
    surfaced="router only (gates.router=allow, gates.phase=deny)",
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
        "context. Cannot touch startup config (reyn.yaml is restart-only). "
        "For the full on: vocabulary, matcher, composers:, emit_hook_event, and every "
        "scheme's fields, see the hooks concept doc at docs/concepts/runtime/hooks.md."
    ),
    ja=(
        "エージェントのライフサイクルポイント（turn_end の自己継続や"
        "コンテキスト注入など）にプッシュフックを追加する。フックは"
        "ランタイムの hooks レイヤー（.reyn/config/hooks.yaml）に書き込まれ、"
        "次のターン境界で適用される（既存フックに追加される）。自己主導"
        "の継続や定期的なコンテキスト注入に使う。起動時設定"
        "（reyn.yaml、再起動時のみ反映）には触れられない。"
        "on: の全語彙・matcher・composers:・emit_hook_event・各スキームの"
        "全フィールドの完全な構文は docs/concepts/runtime/hooks.md（hooks コンセプト"
        "ドキュメント）を参照。"
    ),
)

emit_hook_event = ToolDescription(
    tool_name="emit_hook_event",
    surfaced="router only (gates.router=allow, gates.phase=deny)",
    purpose=(
        "Let the agent emit its OWN llm:<session_id>:<event_name> hook-event "
        "onto this session's HookBus, so a Composer / a hook registered "
        "on: llm:<event_name> can react to it. Structurally session-scoped — "
        "there is no way to name a different session or namespace."
    ),
    text=(
        "Emit a hook-event named event_name, scoped to YOUR OWN session "
        "(the actual kind is llm:<your-session-id>:<event_name> — you cannot "
        "target another session or another namespace like builtin:*/composed:*/"
        "webhook:*). Use this to signal completion of something a Composer or "
        "a hooks_add-registered hook is watching for. payload is an optional "
        "dict carried on the event for a matcher/Composer to inspect."
    ),
    ja=(
        "event_name という名前の hook-event を、あなた自身のセッションに"
        "スコープして（実際の kind は llm:<あなたのセッション ID>:<event_name>"
        "— 他セッションや builtin:*/composed:*/webhook:* 等の他 namespace は"
        "対象にできない）発行する。Composer や hooks_add で登録したフックが"
        "待っている完了シグナルを送るのに使う。payload は matcher/Composer が"
        "参照できる任意の dict。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "hooks_add": hooks_add,
    "emit_hook_event": emit_hook_event,
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
    "emit_hook_event": {
        "event_name": ParamDescription(
            text=(
                "The event's name (becomes the llm:<your-session-id>:<event_name> "
                "kind — you supply only this suffix, never the session component)."
            ),
            ja=(
                "イベント名（llm:<あなたのセッション ID>:<event_name> の kind になる"
                "— あなたが指定できるのはこの suffix のみで、セッション部分は指定不可）。"
            ),
        ),
        "payload": ParamDescription(
            text="Optional dict of fields carried on the event for a matcher/Composer to read.",
            ja="matcher/Composer が読める、イベントに付随する任意の dict フィールド。",
        ),
    },
}
