"""`reyn agent {list,new,rm,show}` — manage persistent agents.

PR10 introduces multi-agent: each agent is a long-lived Session with its
own history under `.reyn/agents/<name>/`. The `default` agent is auto-created
on first use; users can spin up additional named agents with their own role
prompts via `reyn agent new`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import DEFAULT_AGENT_NAME, AgentRegistry, _validate_agent_name


def register(sub) -> None:
    p = sub.add_parser(
        "agent", help="Manage persistent agents (multi-agent: PR10)",
    )
    inner = p.add_subparsers(dest="agent_cmd", metavar="<agent_cmd>")
    inner.required = True

    p_list = inner.add_parser("list", help="List agents")
    p_list.add_argument(
        "--all", action="store_true",
        help="Include archived agents (marked '(archived)'). Default hides them.",
    )
    p_list.set_defaults(func=_cmd_list)

    p_new = inner.add_parser("new", help="Create a new agent")
    p_new.add_argument("name", help="Agent name (a-z 0-9 _ - up to 32 chars)")
    p_new.add_argument(
        "--role", default="",
        help="Free-form role prompt injected into the agent's system prompt",
    )
    p_new.set_defaults(func=_cmd_new)

    p_rm = inner.add_parser(
        "rm", help="Archive an agent (soft-delete; --purge to hard-delete)",
    )
    p_rm.add_argument("name", help="Agent name to remove")
    p_rm.add_argument(
        "--purge", action="store_true",
        help="Hard-delete: destroy rewind history (default archives — "
             "recoverable via time-travel within the retention window)",
    )
    p_rm.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt",
    )
    p_rm.set_defaults(func=_cmd_rm)

    p_show = inner.add_parser("show", help="Print an agent's profile")
    p_show.add_argument("name", help="Agent name")
    p_show.set_defaults(func=_cmd_show)


def _agents_dir() -> Path:
    return Path.cwd() / ".reyn" / "agents"


def _cmd_list(args: argparse.Namespace) -> None:
    base = _agents_dir()
    if not base.is_dir():
        print("(no agents yet — `reyn chat` will auto-create `default`)")
        return
    # #1954: hide archived agents by default — consistent with routing / A2A /
    # the TUI Agents tab (all use ``list_active_names``) + the documented intent
    # (agent.md: "Archived agents are hidden"). ``--all`` reveals them marked so
    # an operator can still see / recover / purge them. Use the canonical
    # registry seam rather than re-deriving the archive marker here.
    show_all = bool(getattr(args, "all", False))

    def _no_factory(profile):  # pragma: no cover — never invoked for a read-only list
        raise RuntimeError("session factory not used in agent CLI")

    reg = AgentRegistry(project_root=Path.cwd(), session_factory=_no_factory)
    active = set(reg.list_active_names())

    rows: list[tuple[str, str, str]] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        try:
            profile = AgentProfile.load(entry)
        except FileNotFoundError:
            continue
        archived = profile.name not in active
        if archived and not show_all:
            continue  # default view hides archived agents
        role_first_line = (profile.role or "").strip().splitlines()
        role_excerpt = role_first_line[0] if role_first_line else ""
        # Last activity = max mtime across history.jsonl + chat events tree.
        latest = 0.0
        history = entry / "history.jsonl"
        if history.is_file():
            latest = max(latest, history.stat().st_mtime)
        # PR20: events live under .reyn/events/agents/<name>/chat/...
        events_root = (
            entry.parent.parent / "events" / "agents" / profile.name / "chat"
        )
        if events_root.is_dir():
            for ef in events_root.rglob("*.jsonl"):
                try:
                    latest = max(latest, ef.stat().st_mtime)
                except OSError:
                    continue
        if latest:
            from datetime import datetime, timezone
            ts = datetime.fromtimestamp(latest, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        else:
            ts = "—"
        name_display = f"{profile.name} (archived)" if archived else profile.name
        rows.append((name_display, ts, role_excerpt[:60]))
    if not rows:
        print("(no agents yet — `reyn chat` will auto-create `default`)")
        return
    name_w = max(len(n) for n, _, _ in rows)
    print(f"{'NAME':<{name_w}}  {'LAST ACTIVITY':<17}  ROLE")
    for n, ts, role in rows:
        print(f"{n:<{name_w}}  {ts:<17}  {role}")


def _cmd_new(args: argparse.Namespace) -> None:
    try:
        _validate_agent_name(args.name)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    base = _agents_dir()
    target = base / args.name
    if target.exists():
        print(f"Error: agent {args.name!r} already exists at {target}", file=sys.stderr)
        sys.exit(1)
    profile = AgentProfile.new(name=args.name, role=args.role)
    profile.save(target)
    print(f"Created agent {args.name!r} at {target}")
    if args.role:
        print(f"  role: {args.role.strip().splitlines()[0]}")
    print(f"  attach with: reyn chat {args.name}")


def _cmd_rm(args: argparse.Namespace) -> None:
    if args.name == DEFAULT_AGENT_NAME:
        print("Error: cannot remove the default agent", file=sys.stderr)
        sys.exit(1)
    target = _agents_dir() / args.name
    if not target.is_dir():
        print(f"Error: agent {args.name!r} not found at {target}", file=sys.stderr)
        sys.exit(1)
    purge = args.purge
    if not args.yes:
        prompt = (
            f"Hard-delete agent {args.name!r} and ALL its rewind history "
            "(irreversible)? [y/N]: "
            if purge
            else f"Archive agent {args.name!r}? (recoverable via time-travel "
                 "within the retention window) [y/N]: "
        )
        try:
            ans = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if ans.strip().lower() != "y":
            print("aborted")
            return
    # Route through AgentRegistry so PR12 topology cascade fires. Attach the WAL
    # (a read-only scan sets current_seq) so an archive records an accurate
    # archival seq — slice-2's WAL-window GC hinge (#1954).
    def _no_factory(profile):
        raise RuntimeError("session factory not used in agent CLI")
    wal_path = Path.cwd() / ".reyn" / "state" / "wal.jsonl"
    state_log = StateLog(wal_path) if wal_path.is_file() else None
    reg = AgentRegistry(
        project_root=Path.cwd(), session_factory=_no_factory, state_log=state_log,
    )
    reg.remove(args.name, purge=purge)
    print(f"{'Purged' if purge else 'Archived'} agent {args.name!r}")


def _cmd_show(args: argparse.Namespace) -> None:
    target = _agents_dir() / args.name
    try:
        profile = AgentProfile.load(target)
    except FileNotFoundError:
        print(f"Error: agent {args.name!r} not found at {target}", file=sys.stderr)
        sys.exit(1)
    print(f"name:        {profile.name}")
    print(f"created_at:  {profile.created_at}")
    print(f"workspace:   {target}")
    # PR15: allowlist visibility.
    if profile.allowed_skills is None:
        print("allowed_skills: (unrestricted — all project + stdlib skills)")
    elif not profile.allowed_skills:
        print("allowed_skills: (none — router-only, no skill spawn)")
    else:
        print("allowed_skills:")
        for s in profile.allowed_skills:
            print(f"  - {s}")
    print("role:")
    if profile.role:
        for line in profile.role.splitlines():
            print(f"  {line}")
    else:
        print("  (empty)")


def run(args: argparse.Namespace) -> None:
    args.func(args)
