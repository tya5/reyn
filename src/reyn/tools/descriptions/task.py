"""Tool descriptions for the ``task`` category — the data-tuple special case.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): the 12 ``task.*`` control-IR ops
(#1953 dynamic-wire item-1) exposed as ``invoke_action`` targets
(``task__create``, ``task__update_status``, …). Unlike every other tool
module, these 12 descriptions previously lived INLINE as the third element
of each ``(op_kind, IROp, description)`` tuple in ``tools/task_ops.py``'s
``_TASK_OPS`` module constant, rather than as standalone ``_X_DESCRIPTION``
constants — this module lifts them out to named ``ToolDescription``
records (matching the convention every other tool file follows), and
``_TASK_OPS`` now references ``TASK_CREATE.text`` etc. in place of the
inline string literal.

``tool_name`` for each entry is the op_kind (e.g. ``"task.create"``) —
this doubles as ``ToolDefinition.name`` for these dynamically-built
definitions (``build_task_tool_definitions`` sets ``name=op_kind``).
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ToolDescription

TASK_CREATE = ToolDescription(
    tool_name="task.create",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Decompose a complex request into trackable sub-units — a task "
        "created while executing a task is auto-owned by it, and "
        "auto-assigned to the creator unless delegated."
    ),
    text=(
        "Create a task. While you are EXECUTING a task, a task you create is automatically "
        "owned by it (a sub-task) and — if you omit `assignee` — assigned to you to execute. "
        "For a TOP-LEVEL task: omitting `assignee` leaves it UNASSIGNED (it waits in the "
        "pending-assignment queue until a session claims it via task.assign); to execute it "
        "YOURSELF, set `assignee` to your own session; set it to another session to delegate. "
        "`deps` are depends-on task ids (born blocked until they complete). Use to decompose a "
        "complex request into trackable units."
    ),
    ja=(
        "タスクを作成する。タスク実行中に作成したタスクは自動的にその"
        "サブタスクとして所有され、`assignee` を省略すれば自分に割り当"
        "てられる。トップレベルタスクの場合、`assignee` を省略すると未"
        "割り当てのまま保留キューで待機する。複雑な依頼を追跡可能な単位"
        "に分解する際に使う。"
    ),
)

TASK_UPDATE_STATUS = ToolDescription(
    tool_name="task.update_status",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Declare a status transition as the single writer (the assignee); "
        "terminal tasks reject further writes."
    ),
    text=(
        "Declare a status transition on a task you are the ASSIGNEE of (the single "
        "writer). Terminal tasks reject writes."
    ),
    ja=(
        "自分が ASSIGNEE（唯一の書き込み者）であるタスクのステータス遷"
        "移を宣言する。終端状態のタスクは書き込みを拒否する。"
    ),
)

TASK_GET = ToolDescription(
    tool_name="task.get",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose="Read one task record by id.",
    text="Read one task record by id.",
    ja="id を指定して1つのタスクレコードを読む。",
)

TASK_LIST = ToolDescription(
    tool_name="task.list",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "List tasks narrowed by assignee/requester/status; narrowing by "
        "requester lists the sub-tasks that task owns."
    ),
    text=(
        "List tasks, optionally narrowed by assignee / requester / status. Narrowing "
        "by `requester` (a task id) lists the sub-tasks that task owns."
    ),
    ja=(
        "タスクを一覧する。assignee / requester / status で絞り込み可"
        "能。`requester`（タスク id）で絞り込むと、そのタスクが所有す"
        "るサブタスクを一覧する。"
    ),
)

TASK_ADD_DEPENDENCY = ToolDescription(
    tool_name="task.add_dependency",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Add a depends-on edge as the requester/topology owner, with "
        "existence + cycle checks."
    ),
    text=(
        "Add a depends-on edge (you must be the requester/topology owner). "
        "Existence + cycle checked."
    ),
    ja=(
        "依存関係エッジを追加する（requester/トポロジー所有者である必要"
        "がある）。存在チェックと循環チェックが行われる。"
    ),
)

TASK_REMOVE_DEPENDENCY = ToolDescription(
    tool_name="task.remove_dependency",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Drop a depends-on edge (idempotent); may promote a now-ready "
        "dependent."
    ),
    text=(
        "Drop a depends-on edge (idempotent). Relaxing the graph may promote a "
        "now-ready dependent."
    ),
    ja=(
        "依存関係エッジを削除する（冪等）。グラフを緩めることで、条件が"
        "揃った依存先タスクが実行可能に昇格することがある。"
    ),
)

TASK_REPOINT_DEPENDENCY = ToolDescription(
    tool_name="task.repoint_dependency",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Atomically repoint a dependency edge to a substitute task — the "
        "primary recovery move, cycle-checked before any mutation."
    ),
    text=(
        "Atomically repoint a dependency edge from one task to a substitute (the "
        "primary recovery move). The new edge is cycle-checked before any mutation."
    ),
    ja=(
        "依存関係エッジを別のタスクへアトミックに付け替える（主要な復"
        "旧手段）。新しいエッジは変更前に循環チェックされる。"
    ),
)

TASK_ABORT = ToolDescription(
    tool_name="task.abort",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Abort (delete) a requested task + its sub-tree; cooperative-"
        "terminal, rejecting the assignee's next status-write."
    ),
    text=(
        "Abort (delete) a task you requested + its sub-tree. Cooperative-terminal: "
        "the assignee's in-flight work is rejected at its next status-write."
    ),
    ja=(
        "自分が requester であるタスクとそのサブツリーを中断（削除）す"
        "る。協調的終端: assignee の進行中の作業は次のステータス書き込"
        "み時に拒否される。"
    ),
)

TASK_HEARTBEAT = ToolDescription(
    tool_name="task.heartbeat",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Liveness ping for a blocked task, triggering unblock-predicate "
        "evaluation and returning current state."
    ),
    text=(
        "Liveness ping for a blocked task; triggers unblock-predicate evaluation. "
        "Returns the current state."
    ),
    ja=(
        "ブロック中のタスクへの生存確認。unblock-predicate の評価をトリ"
        "ガーし、現在の状態を返す。"
    ),
)

TASK_REGISTER_UNBLOCK_PREDICATE = ToolDescription(
    tool_name="task.register_unblock_predicate",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Register a deterministic (code, no-LLM) unblock predicate "
        "evaluated at heartbeat time."
    ),
    text=(
        "Register a deterministic (code, no-LLM) unblock predicate evaluated at "
        "heartbeat; true → unblock."
    ),
    ja=(
        "heartbeat 時に評価される決定的（コード、LLM 不使用）な "
        "unblock predicate を登録する。true でブロック解除。"
    ),
)

TASK_COMMENT = ToolDescription(
    tool_name="task.comment",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Append a comment to a task's thread — the durable inter-agent / "
        "human-in-the-loop protocol."
    ),
    text=(
        "Append a comment to a task's thread (durable inter-agent / human-in-the-loop "
        "protocol)."
    ),
    ja=(
        "タスクのスレッドにコメントを追記する（永続的なエージェント間 / "
        "human-in-the-loop プロトコル）。"
    ),
)

TASK_ASSIGN = ToolDescription(
    tool_name="task.assign",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Claim an UNASSIGNED task, or hand off an already-assigned one "
        "(owner-initiated only) — the new assignee is woken to execute it."
    ),
    text=(
        "Assign a session to a task. An UNASSIGNED task (in the pending-assignment queue) "
        "may be CLAIMED by anyone — set `assignee` to the session that will execute it. An "
        "already-assigned task may be reassigned ONLY by its current assignee (owner-initiated "
        "hand-off; others must request it via conversation). The new assignee is woken to "
        "execute it."
    ),
    ja=(
        "セッションをタスクに割り当てる。未割り当てタスク（保留キュー"
        "内）は誰でも claim 可能。既に割り当て済みのタスクは現在の "
        "assignee のみが再割り当てできる（オーナー主導のハンドオフ）。"
        "新しい assignee は実行のために起こされる。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "task.create": TASK_CREATE,
    "task.update_status": TASK_UPDATE_STATUS,
    "task.get": TASK_GET,
    "task.list": TASK_LIST,
    "task.add_dependency": TASK_ADD_DEPENDENCY,
    "task.remove_dependency": TASK_REMOVE_DEPENDENCY,
    "task.repoint_dependency": TASK_REPOINT_DEPENDENCY,
    "task.abort": TASK_ABORT,
    "task.heartbeat": TASK_HEARTBEAT,
    "task.register_unblock_predicate": TASK_REGISTER_UNBLOCK_PREDICATE,
    "task.comment": TASK_COMMENT,
    "task.assign": TASK_ASSIGN,
}
