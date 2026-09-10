"""Tool descriptions for the ``execution`` category.

Phase 2 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): every ``execution``-category
ToolDefinition's description string lives here as a reviewable
``ToolDescription`` record. Each ``.text`` value is copied verbatim from
its origin tool module; the origin module now aliases its
``_X_DESCRIPTION`` module constant to ``execution.NAME.text`` so every
call site is unchanged.

Covers: the ``exec`` tool (``exec.py``, renamed from ``sandboxed_exec``
#3226 Phase 3 — the op_runtime kind stays ``sandboxed_exec``, only the
tool/qualified name changed). #3226 Phase 1: the ``shell`` tool description
this module used to also cover (thin pipeline-DSL sugar over sandboxed_exec,
#2593) was removed along with the tool itself — its only production path
built ``/bin/sh -c <command>``, the sole shell-injection surface in the
codebase.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

exec_ = ToolDescription(
    tool_name="exec",
    surfaced=(
        "router (gates.router=allow) — FP-0034 exec category, always "
        "visible (#4932: no longer visibility-gated on a configured "
        "sandbox backend; isolation state is disclosed in the "
        "description text instead)"
    ),
    purpose=(
        "Execute a command in a sandboxed environment (FP-0017), with the "
        "sandbox policy (filesystem scope) resolved by the OS, not chosen "
        "by the LLM. #3903①: the wall-clock timeout is one policy axis the "
        "LLM MAY extend, up to the operator's own configured ceiling. "
        "#5825①: network is another — a REQUEST, not a grant, gated by an "
        "ask when the sandbox already has it closed."
    ),
    text=(
        "Execute a command in a sandboxed environment (FP-0017). The sandbox "
        "policy (filesystem scope) is the OPERATOR's, resolved by the OS — "
        "it is not chosen here. "
        "Give EXACTLY ONE of argv / cmd — giving both or neither is a tool "
        "error. "
        "argv: command and arguments (argv[0] is the executable). "
        "cmd: a shell command line (e.g. pipes/redirects/chains) — parsed "
        "for policy, then run via the sandbox's shell with the string "
        "unchanged; an unparseable construct (e.g. $(...), backticks, "
        "globs, heredocs) is rejected with a reason, never silently run. "
        "cmd is SYNCHRONOUS ONLY — combining it with collect=\"async\" is a "
        "tool error; use argv for a background run. "
        "timeout: optional — extends the wall-clock timeout past its "
        "operator-configured default, up to the operator's own configured "
        "maximum; a request above that maximum is rejected, and the "
        "rejection names the actual maximum. If you need longer than that, "
        "run it in the background instead (collect=\"async\") — "
        "background work runs on a separate budget from this foreground "
        "wall-clock cap, and you can stop it with cancel_task. "
        "network: optional, default false — REQUEST network access for "
        "this command; not a grant (see the parameter's own description)."
    ),
    ja=(
        "サンドボックス環境内でコマンドを実行する（FP-0017）。サンドボックス"
        "ポリシー（ファイルシステムスコープ）はオペレーターのものとして OS "
        "が解決する（ここで選択するものではない）。"
        "argv と cmd のどちらか一方だけを指定すること — 両方または"
        "どちらも指定しないとツールエラーになる。"
        "argv: コマンドと引数（argv[0] が実行ファイル）。"
        "cmd: シェルのコマンド行（パイプ／リダイレクト／連結など）— ポリシー"
        "判定のために解析した後、元の文字列のままサンドボックス内のシェルで"
        "実行する。解析できない構文（$(...)・バッククォート・glob・heredoc "
        "など）は理由付きで拒否され、無言で実行されることはない。"
        "cmd は同期実行専用 — collect=\"async\" との併用はツールエラーに"
        "なる。バックグラウンド実行には argv を使うこと。"
        "timeout: 任意 — オペレーター設定の既定タイムアウトを、オペレーター"
        "自身が設定した上限まで延長できる。上限を超える要求は拒否され、"
        "拒否時に実際の上限値が示される。それ以上必要な場合はバックグラウン"
        "ドで実行すること（collect=\"async\"）— バックグラウンドの作業はこ"
        "の前景ウォールクロック上限とは別の予算で動作し、cancel_task で停止"
        "できる。"
        "network: 任意、既定 false — このコマンドのネットワークアクセスを"
        "要求する。付与ではない（パラメータ自身の説明を参照）。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "exec": exec_,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────
#
# #3962: the "timeout" entry this dict used to carry was removed along with
# the tool parameter it described — the wall-clock cap was never actually
# settable via the op on the real path (ctx.default_sandbox_policy's own
# timeout_seconds always governed), so the LLM-facing parameter and its
# description were pure advertised-but-ignored surface.
#
# #3903① (2026-08-11): "timeout" is back, with a real reader this time
# (op_runtime/sandboxed_exec.py). Deliberately no number in this text —
# lead-coder ruling: the operator's configured default/max are per-
# deployment config values, not something safe to bake into a static
# schema string (the exact "restate a count instead of reading the
# registry" drift class named #4158/#4160/#4169/proposal-0067 tonight —
# the schema would silently lie the moment an operator changed the
# config). The authoritative numbers reach the LLM at the moment it needs
# them: the rejection error (op_runtime/sandboxed_exec.py) names the
# ACTUAL configured maximum when a request exceeds it. Dynamically
# injecting the resolved numbers into this static text would need new
# schema-enrichment infra `exec`'s catalog-dispatch path doesn't have
# today (schema_enricher only reaches router_tools.build_tools()'s
# ToolSpec path, not universal_catalog's describe path) — explicitly
# deferred, not silently dropped: file an issue if/when that infra is
# actually needed.

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "exec": {
        "argv": ParamDescription(
            text="Command and arguments; argv[0] is the executable.",
            ja="コマンドと引数。argv[0] が実行ファイル。",
        ),
        # #5838 段6: XOR with argv (exactly one of the two must be given —
        # enforced as a tool error by `_handle`, never a JSON Schema
        # oneOf/anyOf — see exec.py's own comment for why). cmd-mode is
        # sync-only: combined with collect="async" it is also a tool
        # error (run_exec_async has no cmd parameter, #5838 段6 scope).
        "cmd": ParamDescription(
            text=(
                "A shell command line (pipes/redirects/chains), XOR argv. "
                "Parsed for policy, then the ORIGINAL string is run via "
                "the sandbox's shell unchanged. An unparseable construct "
                "($(...), backticks, globs, heredocs, and other "
                "unsupported shell syntax) is rejected with a reason, "
                "never silently run. Sync-only — not usable with "
                "collect=\"async\" (use argv there instead)."
            ),
            ja=(
                "シェルのコマンド行（パイプ／リダイレクト／連結など）、"
                "argv との XOR。ポリシー判定のために解析した後、元の文字列"
                "のままサンドボックス内のシェルで実行する。解析できない構文"
                "（$(...)・バッククォート・glob・heredoc など、サポート対象"
                "外のシェル構文）は理由付きで拒否され、無言で実行されること"
                "はない。同期実行専用 — collect=\"async\" では使えない"
                "（その場合は argv を使うこと）。"
            ),
        ),
        "timeout": ParamDescription(
            text=(
                "Optional wall-clock timeout override in seconds, extending "
                "the operator-configured default. Capped at the operator's "
                "own configured maximum — a request above it is rejected, "
                "and the rejection names the actual maximum."
            ),
            ja=(
                "任意のウォールクロックタイムアウト（秒）— オペレーター設定"
                "の既定値を延長する。オペレーター自身が設定した上限で頭打ち"
                "になり、上限を超える要求は拒否され、拒否時に実際の上限値が"
                "示される。"
            ),
        ),
        # #4733: optional, one declared value ("async"). Omitting it keeps
        # exec synchronous — byte-identical to before this parameter
        # existed; there is no separate "attached" value to declare (sync
        # IS what omission already means). Deliberately terse — the main
        # tool description (`exec_.text` above) already spells out the
        # describe_task/task_settled/cancel_task shape; re-stating it here
        # in full (in BOTH languages) would be pure duplicated token
        # weight on every catalog listing, the exact thing a tight-window
        # turn (tests/runtime/test_intervention_subscriber_guard.py) can't
        # absorb.
        "collect": ParamDescription(
            text='Optional — "async" runs this in the background (see the tool description).',
            ja='任意 — "async" でバックグラウンド実行（ツール説明を参照）。',
        ),
        # #5825① (2026-09-06): a REQUEST, not a grant. Omitted/false is the
        # default and changes nothing. true against a policy that already
        # has network closed may prompt the operator once (or be silently
        # pre-approved/denied by config or a prior persisted answer) before
        # the command runs — never a silent open.
        "network": ParamDescription(
            text=(
                "Optional, default false — REQUEST that this command run "
                "with network access. Not a grant: if the sandbox already "
                "has network closed, this may prompt the operator once "
                "(or be silently pre-approved/denied per prior "
                "configuration) before the command runs."
            ),
            ja=(
                "任意、既定 false — このコマンドをネットワークアクセスあり"
                "で実行することを要求する。付与ではない: サンドボックスが"
                "既にネットワークを閉じている場合、コマンド実行前にオペレ"
                "ーターへ一度確認が入ることがある（または事前設定により無"
                "言で承認／拒否される）。"
            ),
        ),
    },
}
