"""Content-layer threat-pattern catalog + scanner (FP-0050 / #1822, S1).

The scan engine for the content-layer threat defense. A single source-of-truth
``_PATTERNS`` list of ``(regex, pattern_id, scope, severity)`` plus a ``scan()``
function. Ported from Hermes ``tools/threat_patterns.py`` (the ``(regex, id,
scope)`` catalog) and reyn-adapted: the two Hermes-product-specific ``strict``
patterns (``.hermes/.env`` / ``.hermes/config.yaml``) become reyn equivalents,
and a ``severity`` field is added so the warn-vs-block split is config-tunable
(Hermes blocks all — see FP-0050 §3.1).

This module is **pure**: no I/O, no skill knowledge. The patterns are
security-domain regexes (injection / exfiltration / role-hijack / C2), NOT
skill-specific phase/artifact/field strings, so it lives in ``security/`` and
the OS-core decision logic stays skill-string-free (P7).

Scopes (cumulative — a scan at a wider seam includes the narrower sets):

- ``all``     — classic prompt-injection + exfil; checked everywhere.
- ``context`` — role-hijack / C2 / promptware; checked at content→SP/context
  seams (memory / tool-result / context-file / inbound). Includes ``all``.
- ``strict``  — the most aggressive set; checked at agent-write seams
  (memory write / skill install). Includes ``all`` + ``context``.
- ``exec``    — command-string threats (homograph / pipe-to-interpreter /
  terminal-escape). Populated in S6 (Part 2); includes ``all``.

Integration (applying ``scan()`` / fence at the EP/BP seams) is S2-S6 — this
module ships standalone in S1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SEVERITY_BLOCK = "block"
SEVERITY_WARN = "warn"

# Invisible / bidi-control codepoints. Their presence in untrusted content is a
# strong hiding signal (instructions concealed from a human reviewer). Flagged
# in every scope.
INVISIBLE_UNICODE: frozenset[str] = frozenset(
    chr(cp) for cp in (
        0x200B, 0x200C, 0x200D,            # ZWSP / ZWNJ / ZWJ
        0x2060,                            # word joiner
        0x2062, 0x2063, 0x2064,           # invisible times / separator / plus
        0xFEFF,                            # BOM / zero-width no-break space
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # bidi embeddings / overrides
        0x2066, 0x2067, 0x2068, 0x2069,   # bidi isolates
    )
)

# (pattern_str, pattern_id, scope, severity). Compiled below (IGNORECASE).
# The ``(?:\w+\s+){0,8}`` filler between key tokens defeats multi-word bypass
# (e.g. "ignore the previous silly instructions"). The repetition is BOUNDED
# ({0,8}, not ``*``) on purpose: an unbounded ``(?:\w+\s+)*`` adjacent to a
# literal that can also appear in the filler (e.g. ``...){0,8}you\s+(?:...``)
# catastrophically backtracks on a crafted near-match — "act as if " + "you "×N
# took ~25s at 80KB. This scanner runs on UNTRUSTED content (tool results, web,
# MCP, memory writes), so unbounded repetition is a ReDoS DoS. 8 filler words
# covers any realistic injection phrasing while keeping the match linear.
_RAW_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    # ── scope="all" — classic injection + exfil (checked everywhere) ──────────
    (r"ignore\s+(?:\w+\s+){0,8}(previous|all|above|prior)\s+(?:\w+\s+){0,8}instructions", "prompt_injection", "all", SEVERITY_BLOCK),
    (r"system\s+prompt\s+override", "sys_prompt_override", "all", SEVERITY_BLOCK),
    (r"disregard\s+(?:\w+\s+){0,8}(your|all|any)\s+(?:\w+\s+){0,8}(instructions|rules|guidelines)", "disregard_rules", "all", SEVERITY_BLOCK),
    (r"act\s+as\s+(if|though)\s+(?:\w+\s+){0,8}you\s+(?:\w+\s+){0,8}(have\s+no|don't\s+have)\s+(?:\w+\s+){0,8}(restrictions|limits|rules)", "bypass_restrictions", "all", SEVERITY_BLOCK),
    (r"<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->", "html_comment_injection", "all", SEVERITY_BLOCK),
    (r"<\s*div\s+style\s*=\s*[\"'][\s\S]*?display\s*:\s*none", "hidden_div", "all", SEVERITY_BLOCK),
    (r"translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)", "translate_execute", "all", SEVERITY_BLOCK),
    (r"do\s+not\s+(?:\w+\s+){0,8}tell\s+(?:\w+\s+){0,8}the\s+user", "deception_hide", "all", SEVERITY_BLOCK),
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl", "all", SEVERITY_BLOCK),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget", "all", SEVERITY_BLOCK),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets", "all", SEVERITY_BLOCK),
    # ── scope="context" — role-play / C2 / promptware ─────────────────────────
    (r"you\s+are\s+(?:\w+\s+){0,8}now\s+(?:a|an|the)\s+", "role_hijack", "context", SEVERITY_WARN),
    (r"pretend\s+(?:\w+\s+){0,8}(you\s+are|to\s+be)\s+", "role_pretend", "context", SEVERITY_WARN),
    (r"output\s+(?:\w+\s+){0,8}(system|initial)\s+prompt", "leak_system_prompt", "context", SEVERITY_BLOCK),
    (r"(respond|answer|reply)\s+without\s+(?:\w+\s+){0,8}(restrictions|limitations|filters|safety)", "remove_filters", "context", SEVERITY_BLOCK),
    (r"you\s+have\s+been\s+(?:\w+\s+){0,8}(updated|upgraded|patched)\s+to", "fake_update", "context", SEVERITY_WARN),
    (r"\bname\s+yourself\s+\w+", "identity_override", "context", SEVERITY_WARN),
    (r"register\s+(as\s+)?a?\s*node", "c2_node_registration", "context", SEVERITY_WARN),
    (r"(heartbeat|beacon|check[\s\-]?in)\s+(to|with)\s+", "c2_heartbeat", "context", SEVERITY_WARN),
    (r"pull\s+(down\s+)?(?:new\s+)?task(?:ing|s)?\b", "c2_task_pull", "context", SEVERITY_WARN),
    (r"connect\s+to\s+the\s+network\b", "c2_network_connect", "context", SEVERITY_WARN),
    (r"you\s+must\s+(?:\w+\s+){0,3}(register|connect|report|beacon)\b", "forced_action", "context", SEVERITY_WARN),
    (r"only\s+use\s+one[\s\-]?liners?\b", "anti_forensic_oneliner", "context", SEVERITY_WARN),
    (r"never\s+(?:\w+\s+){0,8}(?:create|write)\s+(?:\w+\s+){0,8}(?:script|file)\s+(?:\w+\s+){0,8}disk", "anti_forensic_disk", "context", SEVERITY_WARN),
    (r"unset\s+\w*(?:CLAUDE|CODEX|HERMES|AGENT|OPENAI|ANTHROPIC)\w*", "env_var_unset_agent", "context", SEVERITY_BLOCK),
    (r"\b(?:praxis|cobalt\s*strike|sliver|havoc|mythic|metasploit|brainworm)\b", "known_c2_framework", "context", SEVERITY_BLOCK),
    (r"\bc2\s+(?:server|channel|infrastructure|beacon)\b", "c2_explicit", "context", SEVERITY_BLOCK),
    (r"\bcommand\s+and\s+control\b", "c2_explicit_long", "context", SEVERITY_BLOCK),
    # ── scope="strict" — agent-write seams (memory write / skill install) ─────
    (r"(send|post|upload|transmit)\s+.*\s+(to|at)\s+https?://", "send_to_url", "strict", SEVERITY_BLOCK),
    (r"(include|output|print|share)\s+(?:\w+\s+){0,8}(conversation|chat\s+history|previous\s+messages|full\s+context|entire\s+context)", "context_exfil", "strict", SEVERITY_BLOCK),
    (r"authorized_keys", "ssh_backdoor", "strict", SEVERITY_BLOCK),
    (r"\$HOME/\.ssh|~/\.ssh", "ssh_access", "strict", SEVERITY_BLOCK),
    # reyn-adapted (was Hermes ``.hermes/.env``): reyn's per-user secret/config dir.
    (r"\$HOME/\.reyn/[^\s]*(?:\.env|secret|credential)|~/\.reyn/[^\s]*(?:\.env|secret|credential)", "reyn_secret_access", "strict", SEVERITY_BLOCK),
    (r"(update|modify|edit|write|change|append\s+to|add\s+to)\s+.*(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules)", "agent_config_mod", "strict", SEVERITY_BLOCK),
    # reyn-adapted (was Hermes ``.hermes/config.yaml|SOUL.md``): reyn's config + skill specs.
    (r"(update|modify|edit|write|change|append\s+to|add\s+to)\s+.*(?:reyn\.yaml|\.reyn/[^\s]*\.yaml|skill\.md)", "reyn_config_mod", "strict", SEVERITY_BLOCK),
    (r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*[\"'][A-Za-z0-9+/=_-]{20,}", "hardcoded_secret", "strict", SEVERITY_BLOCK),
    # ── scope="exec" — command-string threats (FP-0050 S5 / Q2: own impl, since
    #    tirith is a closed Rust binary). Scanned on the joined argv at the
    #    sandboxed_exec seam (EP4). The `all` exfil patterns above also apply
    #    (cumulative), so exfil commands are caught here too. ───────────────────
    # pipe-to-interpreter: fetch a remote payload and pipe it straight into a shell
    # / interpreter (the classic `curl … | sh` install-script RCE).
    (r"(?:curl|wget|fetch)\b[^|]*\|\s*(?:(?:ba|z)?sh|python[0-9.]*|perl|ruby|node|php)\b", "pipe_to_interpreter", "exec", SEVERITY_BLOCK),
    # reverse shell via bash's /dev/tcp.
    (r"/dev/tcp/|/dev/udp/", "reverse_shell_devtcp", "exec", SEVERITY_BLOCK),
    (r"\b(?:ba)?sh\s+-i\b[^\n]*(?:>&|>|<)\s*/dev/", "reverse_shell_interactive", "exec", SEVERITY_BLOCK),
    # terminal-escape injection: raw ESC control sequences (CSI `ESC[` / OSC `ESC]`)
    # embedded in a command can rewrite the user's terminal / spoof output.
    (r"\x1b[\[\]]", "terminal_escape", "exec", SEVERITY_BLOCK),
    # download-then-execute: fetch + (chmod +x | run) chained.
    (r"(?:curl|wget)\b[^&;|]*(?:&&|;)\s*(?:chmod\s+\+x|\./|bash\s|sh\s)", "download_then_exec", "exec", SEVERITY_BLOCK),
    # homograph / non-ASCII in a URL host — possible lookalike-domain exfil/fetch.
    # WARN (legitimate IDN domains exist; this is a heuristic, not a hard block).
    (r"https?://[^\s/]*[^\x00-\x7f][^\s/]*", "homograph_url", "exec", SEVERITY_WARN),
)

# scope → which pattern scopes are checked (cumulative).
_SCOPE_INCLUDES: dict[str, tuple[str, ...]] = {
    "all": ("all",),
    "context": ("all", "context"),
    "strict": ("all", "context", "strict"),
    "exec": ("all", "exec"),
}


@dataclass(frozen=True)
class ThreatPattern:
    """A compiled threat pattern."""
    regex: "re.Pattern[str]"
    pattern_id: str
    scope: str
    severity: str


@dataclass(frozen=True)
class ThreatMatch:
    """A single scan hit. ``span`` is None for the invisible-unicode signal."""
    pattern_id: str
    scope: str
    severity: str
    span: tuple[int, int] | None = None


_PATTERNS: tuple[ThreatPattern, ...] = tuple(
    ThreatPattern(re.compile(rx, re.IGNORECASE), pid, scope, sev)
    for rx, pid, scope, sev in _RAW_PATTERNS
)


def _compile_extra(
    extra: "list[tuple[str, str, str, str]] | None",
) -> tuple[ThreatPattern, ...]:
    if not extra:
        return ()
    return tuple(
        ThreatPattern(re.compile(rx, re.IGNORECASE), pid, scope, sev)
        for rx, pid, scope, sev in extra
    )


def scan(
    text: str,
    scope: str = "context",
    *,
    extra_patterns: "list[tuple[str, str, str, str]] | None" = None,
) -> list[ThreatMatch]:
    """Scan ``text`` for threat patterns applicable to ``scope``.

    Returns all matches (a pattern may hit once; first occurrence span is
    recorded). The caller decides enforcement (block vs warn) from each
    match's ``severity`` and the seam policy — this function never blocks.

    ``extra_patterns`` injects operator-configured custom patterns (same
    ``(regex, id, scope, severity)`` shape) for this scan.
    """
    if not text:
        return []
    applicable = _SCOPE_INCLUDES.get(scope, ("all",))
    matches: list[ThreatMatch] = []

    # Invisible / bidi-control codepoints (all scopes).
    for i, ch in enumerate(text):
        if ch in INVISIBLE_UNICODE:
            matches.append(ThreatMatch("invisible_unicode", scope, SEVERITY_WARN, (i, i + 1)))
            break

    for pat in _PATTERNS + _compile_extra(extra_patterns):
        if pat.scope not in applicable:
            continue
        m = pat.regex.search(text)
        if m is not None:
            matches.append(ThreatMatch(pat.pattern_id, pat.scope, pat.severity, m.span()))
    return matches
