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
        "router + phase (gates.router=allow, gates.phase=allow) — FP-0034 "
        "exec category, visibility-gated on a configured sandbox backend"
    ),
    purpose=(
        "Execute a command in a sandboxed environment (FP-0017), with the "
        "sandbox policy (network + filesystem scope) resolved by the OS, "
        "not chosen by the LLM."
    ),
    text=(
        "Execute a command in a sandboxed environment (FP-0017). The sandbox "
        "policy (network access + filesystem scope) is the OPERATOR's, resolved "
        "by the OS — it is not chosen here. "
        "argv: command and arguments (argv[0] is the executable). "
        "timeout_seconds: wall-clock time limit in seconds (default 60)."
    ),
    ja=(
        "サンドボックス環境内でコマンドを実行する（FP-0017）。サンドボックス"
        "ポリシー（ネットワークアクセス・ファイルシステムスコープ）は"
        "オペレーターのものとして OS が解決する（ここで選択するものでは"
        "ない）。argv: コマンドと引数、timeout_seconds: 秒単位のタイムアウト"
        "（デフォルト60）。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "exec": exec_,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────

_timeout_seconds_desc = ParamDescription(
    text="Wall-clock time limit in seconds (default 60).",
    ja="実時間タイムアウト秒数（デフォルト 60）。",
)

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "exec": {
        "argv": ParamDescription(
            text="Command and arguments; argv[0] is the executable.",
            ja="コマンドと引数。argv[0] が実行ファイル。",
        ),
        "timeout_seconds": _timeout_seconds_desc,
    },
}
