"""`reyn topology {list,new,show,rm,add-member,rm-member}` — manage topologies.

PR12 introduces topologies as a first-class abstraction over agent-to-agent
communication. Each topology lives at `.reyn/topologies/<name>.yaml` and
declares its kind (`network` / `team` / `pipeline`) plus members. The
AgentRegistry consults them when routing delegations.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reyn.runtime.registry import _DEFAULT_TOPOLOGY_NAME, AgentRegistry
from reyn.runtime.topology import KINDS, Topology


def register(sub) -> None:
    p = sub.add_parser(
        "topology", help="Manage agent communication topologies (PR12)",
    )
    inner = p.add_subparsers(dest="topology_cmd", metavar="<topology_cmd>")
    inner.required = True

    p_list = inner.add_parser("list", help="List topologies")
    p_list.set_defaults(func=_cmd_list)

    p_new = inner.add_parser("new", help="Create a new topology")
    p_new.add_argument("name", help="Topology name (a-z 0-9 _ - up to 32 chars)")
    p_new.add_argument(
        "--kind", required=True, choices=list(KINDS),
        help="Topology kind: network (free), team (leader-centric), pipeline (sequence)",
    )
    p_new.add_argument(
        "--members", required=True,
        help="Comma-separated agent names (order matters for kind=pipeline)",
    )
    p_new.add_argument(
        "--leader", default=None,
        help="Leader agent (required for kind=team, must be in --members)",
    )
    p_new.set_defaults(func=_cmd_new)

    p_show = inner.add_parser("show", help="Show a topology and its permitted edges")
    p_show.add_argument("name", help="Topology name")
    p_show.set_defaults(func=_cmd_show)

    p_rm = inner.add_parser("rm", help="Remove a topology")
    p_rm.add_argument("name", help="Topology name")
    p_rm.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt",
    )
    p_rm.set_defaults(func=_cmd_rm)

    p_add = inner.add_parser("add-member", help="Add an agent to a topology")
    p_add.add_argument("topology", help="Topology name")
    p_add.add_argument("agent", help="Agent name to add")
    p_add.set_defaults(func=_cmd_add_member)

    p_rmm = inner.add_parser("rm-member", help="Remove an agent from a topology")
    p_rmm.add_argument("topology", help="Topology name")
    p_rmm.add_argument("agent", help="Agent name to remove")
    p_rmm.set_defaults(func=_cmd_rm_member)


def _registry() -> AgentRegistry:
    # CLI never wires session_factory because we don't run sessions here.
    def _no_factory(profile):
        raise RuntimeError("session factory not used in topology CLI")
    return AgentRegistry(project_root=Path.cwd(), session_factory=_no_factory)


def _format_members(topo: Topology) -> str:
    if topo.kind == "team":
        return ", ".join(
            f"{m}*" if m == topo.leader else m for m in topo.members
        )
    if topo.kind == "pipeline":
        return " → ".join(topo.members)
    return ", ".join(topo.members)


def _cmd_list(args: argparse.Namespace) -> None:
    reg = _registry()
    topologies = reg.list_topologies()
    if not topologies:
        print("(no topologies — `reyn topology new <name> --kind ...`)")
        return
    name_w = max(len(t.name) for t in topologies)
    kind_w = max(len(t.kind) for t in topologies)
    print(f"{'NAME':<{name_w}}  {'KIND':<{kind_w}}  MEMBERS")
    for t in topologies:
        print(f"{t.name:<{name_w}}  {t.kind:<{kind_w}}  {_format_members(t)}")


def _cmd_new(args: argparse.Namespace) -> None:
    reg = _registry()
    members = [m.strip() for m in args.members.split(",") if m.strip()]
    if not members:
        print("Error: --members must list at least one agent", file=sys.stderr)
        sys.exit(2)
    if args.kind == "team" and not args.leader:
        print("Error: kind=team requires --leader", file=sys.stderr)
        sys.exit(2)
    if args.kind != "team" and args.leader:
        print(
            f"Error: --leader is only valid for kind=team (got kind={args.kind})",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        topo = Topology.new(
            args.name, kind=args.kind, members=members, leader=args.leader,
        )
        reg.add_topology(topo)
    except (ValueError, FileExistsError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Created topology {args.name!r} ({args.kind}): {_format_members(topo)}")


def _cmd_show(args: argparse.Namespace) -> None:
    reg = _registry()
    try:
        topo = reg.get_topology(args.name)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if topo.name == _DEFAULT_TOPOLOGY_NAME:
        print(
            f"name:        {topo.name}  "
            "(auto-managed; agents not in any user topology)"
        )
    else:
        print(f"name:        {topo.name}")
    print(f"kind:        {topo.kind}")
    if topo.leader is not None:
        print(f"leader:      {topo.leader}")
    members_str = _format_members(topo) if topo.members else "(none)"
    print(f"members:     {members_str}")
    if topo.created_at:
        print(f"created_at:  {topo.created_at}")
    edges = topo.edges()
    print()
    if not edges:
        print("(no permitted edges — topology has fewer than 2 members)")
    else:
        print(f"permitted edges ({len(edges)}):")
        for a, b in edges:
            print(f"  {a} → {b}")


def _cmd_rm(args: argparse.Namespace) -> None:
    reg = _registry()
    if not reg.topology_exists(args.name):
        print(f"Error: topology {args.name!r} not found", file=sys.stderr)
        sys.exit(1)
    if not args.yes:
        try:
            ans = input(f"Remove topology {args.name!r}? [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if ans.strip().lower() != "y":
            print("aborted")
            return
    try:
        reg.remove_topology(args.name)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Removed topology {args.name!r}")


def _cmd_add_member(args: argparse.Namespace) -> None:
    reg = _registry()
    try:
        topo = reg.add_member(args.topology, args.agent)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Added {args.agent!r} to {args.topology!r}: {_format_members(topo)}")


def _cmd_rm_member(args: argparse.Namespace) -> None:
    reg = _registry()
    try:
        topo = reg.remove_member(args.topology, args.agent)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Removed {args.agent!r} from {args.topology!r}: {_format_members(topo)}")


def run(args: argparse.Namespace) -> None:
    args.func(args)
