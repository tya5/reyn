"""Quick MCP-stdio dev probe — talk to `reyn mcp serve` without Claude Desktop.

Run this script to smoke-test the MCP server during development without
having to restart Claude Desktop on every change. Spawns `reyn mcp serve`
as a subprocess, sends a single ``send_to_agent`` tool call, prints the
response (or partial / timeout result).

Usage::

    python scripts/mcp_probe.py                                    # default agent + canned message
    python scripts/mcp_probe.py --agent default --message "hi"     # custom
    python scripts/mcp_probe.py --reyn /path/to/reyn               # pin reyn binary
    python scripts/mcp_probe.py --project /path/to/reyn-project    # pin project root

All flags optional; sensible defaults derive from the script's own
location (= the repo's project root) and the `reyn` on PATH.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _default_reyn() -> str:
    found = shutil.which("reyn")
    if found:
        return found
    print("error: `reyn` not on PATH; pass --reyn /path/to/reyn", file=sys.stderr)
    sys.exit(2)


def _default_project() -> str:
    # repo root = parent of scripts/
    return str(Path(__file__).resolve().parent.parent)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--reyn", default=None, help="Path to the `reyn` binary")
    p.add_argument("--project", default=None, help="Reyn project root")
    p.add_argument("--agent", default="default", help="Agent name (default: default)")
    p.add_argument("--message", default="Reply with one short word.",
                   help="User message to send")
    p.add_argument("--timeout", type=float, default=20.0,
                   help="Server-side timeout (default: 20s)")
    p.add_argument("--wait", type=float, default=30.0,
                   help="Probe-side wait for response (default: 30s)")
    args = p.parse_args()

    reyn_bin = args.reyn or _default_reyn()
    project = args.project or _default_project()

    proc = subprocess.Popen(
        [reyn_bin, "mcp", "serve", "--project", project,
         "--timeout", str(args.timeout)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n"); proc.stdin.flush()

    def recv(t: float) -> str | None:
        ready, _, _ = select.select([proc.stdout], [], [], t)
        return proc.stdout.readline() if ready else None

    # Init
    send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
          "params": {"protocolVersion": "2025-11-25",
                     "capabilities": {},
                     "clientInfo": {"name": "mcp-probe", "version": "0.1.0"}}})
    init = recv(5)
    if not init:
        print("error: server didn't respond to initialize", file=sys.stderr)
        proc.kill()
        proc.wait(); return 1

    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # Tool call
    print(f"send_to_agent({args.agent!r}, {args.message!r})  timeout={args.timeout}s",
          flush=True)
    t0 = time.time()
    send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "send_to_agent",
                     "arguments": {"agent_name": args.agent, "message": args.message}}})

    deadline = time.time() + args.wait
    response_line = None
    while time.time() < deadline:
        r = recv(2)
        if r:
            response_line = r
            break

    elapsed = time.time() - t0
    proc.stdin.close()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    if response_line is None:
        print(f"\nTIMEOUT after {elapsed:.1f}s — server didn't reply within "
              f"--wait={args.wait}s", file=sys.stderr)
        return 1

    payload = json.loads(response_line)
    inner = payload.get("result", {}).get("content", [{}])[0].get("text", "")
    try:
        parsed = json.loads(inner)
    except json.JSONDecodeError:
        parsed = {"reply": inner}

    print(f"\n[{elapsed:.1f}s] reply={parsed.get('reply')!r}  "
          f"partial={parsed.get('partial')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
