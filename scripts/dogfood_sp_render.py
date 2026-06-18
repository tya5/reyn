"""dogfood_sp_render.py — SP rendering confirmation utility for batch dogfood.

Replaces ad-hoc ``python -c "from reyn... build_system_prompt(...)"`` one-liners
with a stable, flag-driven CLI for batch 23+ dogfood observation.

Usage:
    python scripts/dogfood_sp_render.py [flags]

    # Basic render (full SP to stdout):
    python scripts/dogfood_sp_render.py

    # Stats only:
    python scripts/dogfood_sp_render.py --stats
    # => e.g. "2487 chars / 47 lines"

    # Section headers only:
    python scripts/dogfood_sp_render.py --show-sections

    # Legacy literal grep:
    python scripts/dogfood_sp_render.py --grep-legacy

    # With skills and agents:
    python scripts/dogfood_sp_render.py --skill code_review=review_code --skill skill_builder=build
    python scripts/dogfood_sp_render.py --agent-peer planner=plan_and_decompose

    # With MCP servers and indexed sources:
    python scripts/dogfood_sp_render.py --mcp-servers brave=search_the_web
    python scripts/dogfood_sp_render.py --indexed-sources meetings --indexed-sources docs

    # With file scope:
    python scripts/dogfood_sp_render.py --file-scope read=/workspace write=/workspace/out

    # Legacy fixture byte-identity check (future Phase 5 prep):
    python scripts/dogfood_sp_render.py --legacy-check
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Ensure src/ is importable when run from the repo root.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from reyn.runtime.router_system_prompt import build_system_prompt  # noqa: E402

# ---------------------------------------------------------------------------
# Legacy tool literal names — what --grep-legacy checks for.
# ---------------------------------------------------------------------------

_LEGACY_TOOL_LITERALS = [
    "invoke_skill",
    "list_skills",
    "describe_skill",
    "read_local_files",
    "read_file",
    "delegate_to_agent",
    "list_agents",
    "describe_agent",
    "remember_shared",
    "remember_agent",
    "forget_shared",
    "web_search",
    "write_file",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_kv(raw: str, flag: str) -> dict[str, str]:
    """Parse a ``name=value`` string into a dict.

    Raises SystemExit with a usage message on bad format.
    """
    if "=" not in raw:
        sys.exit(
            f"error: --{flag} value must be in name=value form, got: {raw!r}"
        )
    name, _, value = raw.partition("=")
    return {"name": name.strip(), "description": value.strip()}


def _parse_file_scope(raw_list: list[str]) -> dict[str, list[str]]:
    """Parse repeatable ``read=path`` / ``write=path`` values into a file_permissions dict."""
    result: dict[str, list[str]] = {"read": [], "write": []}
    for item in raw_list:
        if "=" not in item:
            sys.exit(
                f"error: --file-scope values must be read=<path> or write=<path>, got: {item!r}"
            )
        key, _, path = item.partition("=")
        key = key.strip()
        path = path.strip()
        if key not in ("read", "write"):
            sys.exit(
                f"error: --file-scope key must be 'read' or 'write', got: {key!r}"
            )
        result[key].append(path)
    return result if (result["read"] or result["write"]) else None  # type: ignore[return-value]


def _parse_indexed_sources(names: list[str]) -> str | None:
    """Build a minimal indexed_sources_section string from a list of source names."""
    if not names:
        return None
    lines = ["## Indexed sources"]
    for name in names:
        lines.append(f"- {name}")
    return "\n".join(lines)


def _build_sp(args: argparse.Namespace) -> str:
    """Construct the system prompt from parsed CLI args."""
    skills = [_parse_kv(s, "skill") for s in (args.skill or [])]
    agents = [_parse_kv(a, "agent_peer") for a in (args.agent_peer or [])]
    mcp_servers = [_parse_kv(m, "mcp-servers") for m in (args.mcp_servers or [])] or None
    file_permissions = _parse_file_scope(args.file_scope or [])
    indexed_sources_section = _parse_indexed_sources(args.indexed_sources or [])

    memory_index: dict = {"status": "not_found", "content": ""}

    return build_system_prompt(
        agent_name=args.agent_name,
        agent_role=args.agent_role,
        available_skills=skills,
        available_agents=agents,
        memory_index=memory_index,
        file_permissions=file_permissions,
        mcp_servers=mcp_servers,
        output_language=args.output_language,
        project_context=args.project_context or "",
        indexed_sources_section=indexed_sources_section,
        universal_wrappers_enabled=args.universal_wrappers_enabled,
    )


# ---------------------------------------------------------------------------
# Output modes
# ---------------------------------------------------------------------------


def _do_stats(sp: str, prefix: str = "") -> None:
    """Print char and line count."""
    chars = len(sp)
    lines = len(sp.split("\n"))
    tag = f"{prefix}: " if prefix else ""
    print(f"{tag}{chars} chars / {lines} lines")


def _do_show_sections(sp: str) -> None:
    """Print all ## section headers."""
    for header in re.findall(r"^##.*$", sp, re.MULTILINE):
        print(header)


def _do_grep_legacy(sp: str) -> None:
    """Check for legacy tool literals remaining in the rendered SP."""
    found: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(sp.split("\n"), start=1):
        for literal in _LEGACY_TOOL_LITERALS:
            if literal in line:
                found.append((lineno, literal, line.rstrip()))

    if not found:
        print("No legacy tool literals found.")
    else:
        print(f"Found {len(found)} legacy tool literal reference(s):")
        for lineno, literal, line in found:
            print(f"  line {lineno}: [{literal}]  {line[:120]}")


def _do_legacy_check(args: argparse.Namespace) -> None:
    """Verify byte-identity against known fixtures.

    Currently a no-op stub (Phase 5 prep) — prints the SP char count and a
    reminder that fixture-pinning requires the 7 LLMReplay fixture files to
    be wired in.  When wired, this mode will load each fixture's expected SP
    and compare byte-for-byte.
    """
    sp = _build_sp(args)
    print(f"Legacy SP rendered: {len(sp)} chars / {len(sp.split(chr(10)))} lines")
    print(
        "Note: --legacy-check fixture comparison is a Phase 5 stub. "
        "Wire the 7 LLMReplay fixture files to enable byte-identity verification."
    )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dogfood_sp_render.py",
        description=(
            "SP rendering confirmation utility for batch dogfood.\n"
            "Default: render full system prompt to stdout.\n"
            "Use --stats / --show-sections / --grep-legacy for targeted inspection."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Core SP build flags ────────────────────────────────────────────────
    parser.add_argument(
        "--universal-wrappers-enabled",
        action="store_true",
        default=True,
        help="Enable universal wrapper tools (default: True).",
    )
    parser.add_argument(
        "--no-universal-wrappers-enabled",
        dest="universal_wrappers_enabled",
        action="store_false",
        help="Disable universal wrappers.",
    )
    parser.add_argument(
        "--agent-name",
        default="default",
        metavar="NAME",
        help="Agent identifier (default: 'default').",
    )
    parser.add_argument(
        "--agent-role",
        default="generalist",
        metavar="ROLE",
        help="Agent role one-liner (default: 'generalist').",
    )

    # ── Repeatable multi-value flags ───────────────────────────────────────
    parser.add_argument(
        "--skill",
        action="append",
        metavar="NAME=DESC",
        help=(
            "Available skill in name=description form. "
            "Repeatable (e.g. --skill code_review=review_code --skill skill_builder=build)."
        ),
    )
    parser.add_argument(
        "--agent-peer",
        action="append",
        metavar="NAME=ROLE",
        help=(
            "Peer agent in name=role form. "
            "Repeatable (e.g. --agent-peer planner=plan_and_decompose)."
        ),
    )
    parser.add_argument(
        "--mcp-servers",
        action="append",
        metavar="NAME=DESC",
        help=(
            "MCP server in name=description form. "
            "Repeatable (e.g. --mcp-servers brave=search_the_web)."
        ),
    )
    parser.add_argument(
        "--indexed-sources",
        action="append",
        metavar="NAME",
        help=(
            "Indexed source name (plain string). "
            "Repeatable (e.g. --indexed-sources meetings --indexed-sources docs)."
        ),
    )
    parser.add_argument(
        "--file-scope",
        action="append",
        metavar="read=PATH|write=PATH",
        help=(
            "File permission entry in read=<path> or write=<path> form. "
            "Repeatable (e.g. --file-scope read=/workspace --file-scope write=/workspace/out)."
        ),
    )

    # ── Optional SP content flags ──────────────────────────────────────────
    parser.add_argument(
        "--output-language",
        metavar="LANG",
        default=None,
        help="BCP-47 language code for the SP language directive (e.g. 'ja', 'en'). Default: unset.",
    )
    parser.add_argument(
        "--project-context",
        metavar="TEXT",
        default=None,
        help="Free-text project context injected into the SP.",
    )

    # ── Output mode flags (mutually exclusive) ─────────────────────────────
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--stats",
        action="store_true",
        default=False,
        help="Print char count / line count only (no full render).",
    )
    mode_group.add_argument(
        "--show-sections",
        action="store_true",
        default=False,
        help="Print ## section headers only.",
    )
    mode_group.add_argument(
        "--grep-legacy",
        action="store_true",
        default=False,
        help=(
            "Check rendered SP for legacy tool literal residue "
            "(" + ", ".join(_LEGACY_TOOL_LITERALS[:4]) + ", ...). "
            "Exits 0 if none found, 1 if any found."
        ),
    )
    mode_group.add_argument(
        "--legacy-check",
        action="store_true",
        default=False,
        help=(
            "Verify hide_legacy_tools=False output against known LLMReplay fixtures. "
            "Phase 5 stub — currently prints stats + reminder."
        ),
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for SP rendering confirmation utility."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.legacy_check:
        _do_legacy_check(args)
        return

    # ── All other modes: build one SP ──────────────────────────────────────
    sp = _build_sp(args)

    if args.stats:
        _do_stats(sp)
        return

    if args.show_sections:
        _do_show_sections(sp)
        return

    if args.grep_legacy:
        # Count matches to set exit code.
        lines_with_matches = []
        for lineno, line in enumerate(sp.split("\n"), start=1):
            for literal in _LEGACY_TOOL_LITERALS:
                if literal in line:
                    lines_with_matches.append((lineno, literal, line))
                    break
        _do_grep_legacy(sp)
        sys.exit(1 if lines_with_matches else 0)

    # Default: full SP to stdout.
    print(sp)


if __name__ == "__main__":
    main()
