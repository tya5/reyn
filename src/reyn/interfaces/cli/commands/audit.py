"""`reyn audit` — static safety scan of installed skills / plugins (#1864, #1822 Part 3).

A STATIC, install-time / on-demand audit (distinct from the #1822 S1-S5 runtime
content-threat scan). It READS skill / plugin code + config and pattern-scans them —
it never imports or executes skill code. The OpenClaw ``audit-*`` analog.

Three rules (lead-greenlit, #1864):
  1. **unsafe code/config** — reuse the #1822 ``threat_patterns`` catalog (static
     scan of each skill/plugin text file at the strict + exec scopes).
  2. **secrets permission** — ``~/.reyn/secrets.env`` is written chmod 600
     (security/secrets/store.py); flag any group/other-accessible deviation.
  3. **gateway exposure** — grounded in real fields. Skills no longer self-declare a
     sandbox policy (#1326 retired it → operator reyn.yaml only), so a skill's
     exposure is inferred from a shipped ``.py`` preprocessor (= executable code,
     HIGH); MCP plugin configs are read directly: a ``command`` (subprocess spawn,
     HIGH), secret-looking ``env`` keys (+secrets, HIGH), a network ``url`` (egress,
     INFO), and every server is enumerated (INFO).

``reyn audit [--skill <name>] [--json]`` → findings; **exit non-zero only on a
block-severity (HIGH) finding** (CI-usable).
"""
from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

# Severity ranks — HIGH is the only one that makes ``reyn audit`` exit non-zero.
_HIGH, _MED, _INFO = "HIGH", "MED", "INFO"
_SECRET_HINT = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PASSWD")
_SCAN_SUFFIXES = (".md", ".py", ".yaml", ".yml", ".json", ".toml", ".sh")


@dataclass
class Finding:
    location: str   # where (skill/plugin + file)
    rule: str       # which rule fired
    severity: str   # HIGH / MED / INFO
    detail: str


def register(sub) -> None:
    p = sub.add_parser(
        "audit",
        help="Static safety scan of installed skills / plugins (code/config + secrets + gateway)",
    )
    p.add_argument("--skill", default=None, help="Audit only this skill by name.")
    p.add_argument("--json", action="store_true", help="Emit findings as JSON.")
    p.set_defaults(func=run)


def _skill_dirs(only: str | None) -> list[Path]:
    """INTRODUCED skill directories — ``reyn/project`` + ``reyn/local`` only. The
    stdlib (``src/stdlib/skills``) is first-party/trusted and legitimately ships
    .py preprocessors, so it is NOT audited (the issue targets *external/introduced*
    skill/plugin code). Deduped by name (project wins, the resolution order)."""
    roots = [Path("reyn") / "project", Path("reyn") / "local"]
    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in seen or not (d / "skill.md").exists():
                continue
            if only is not None and d.name != only:
                continue
            seen.add(d.name)
            out.append(d)
    return out


def _scan_text_files(skill_dir: Path) -> list[Finding]:
    """Rule 1: reuse the #1822 threat_patterns catalog statically over each text
    file (strict + exec scopes = the full populated catalog)."""
    from reyn.security.threat_patterns import scan
    findings: list[Finding] = []
    for fp in sorted(skill_dir.rglob("*")):
        if not fp.is_file() or fp.suffix.lower() not in _SCAN_SUFFIXES:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matches = {}  # pattern_id → match (dedup across scopes)
        for scope in ("strict", "exec"):
            for m in scan(text, scope=scope):
                matches[m.pattern_id] = m
        for m in matches.values():
            findings.append(Finding(
                location=f"{skill_dir.name}/{fp.relative_to(skill_dir)}",
                rule="unsafe-code",
                severity=_HIGH if m.severity == "block" else _MED,
                detail=f"matched threat pattern {m.pattern_id!r} ({m.scope})",
            ))
    return findings


# Unsafe python constructs an introduced skill's preprocessor could ship —
# code-execution / escape / egress primitives. Substring match on the source
# (static; the .py is never imported or run). Mere presence of a .py is NOT
# flagged (most skills ship benign preprocessors) — only these constructs are.
_UNSAFE_PY = (
    "subprocess", "os.system", "os.popen", "eval(", "exec(", "__import__",
    "pickle.load", "marshal.load", "socket.", "ctypes", "pty.spawn",
)


def _gateway_skill(skill_dir: Path) -> list[Finding]:
    """Rule 3 (skill side): a shipped .py preprocessor containing an unsafe
    code-execution / escape / egress construct → HIGH gateway:unsafe-python.
    (Static substring scan; the file is never executed.)"""
    findings: list[Finding] = []
    for p in sorted(skill_dir.rglob("*.py")):
        if not p.is_file():
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = sorted({c for c in _UNSAFE_PY if c in src})
        if hits:
            findings.append(Finding(
                location=f"{skill_dir.name}/{p.relative_to(skill_dir).as_posix()}",
                rule="gateway:unsafe-python", severity=_HIGH,
                detail=f"preprocessor uses unsafe construct(s): {hits}",
            ))
    return findings


def _secrets_perm() -> list[Finding]:
    """Rule 2: ~/.reyn/secrets.env must be chmod 600 (store.py convention)."""
    path = Path.home() / ".reyn" / "secrets.env"
    if not path.exists():
        return []
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):  # any group/other bit set
        return [Finding(
            location=str(path), rule="secrets-perm", severity=_HIGH,
            detail=f"group/other-accessible (mode {oct(stat.S_IMODE(mode))}); expected 600",
        )]
    return []


def _gateway_mcp() -> list[Finding]:
    """Rule 3 (plugin side): read MCP server configs (reyn.yaml mcp:) directly —
    command=subprocess (HIGH), secret-looking env (+secrets HIGH), network url
    (egress INFO), and enumerate every server (INFO)."""
    findings: list[Finding] = []
    try:
        from reyn.config import load_config
        mcp = dict(load_config().mcp or {})
    except Exception as exc:  # never let config-load break the audit
        return [Finding(location="reyn.yaml mcp:", rule="gateway:mcp",
                        severity=_INFO, detail=f"could not load mcp config: {exc}")]
    servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else mcp
    for name, cfg in (servers or {}).items():
        if not isinstance(cfg, dict):
            continue
        loc = f"mcp:{name}"
        findings.append(Finding(loc, "gateway:mcp-server", _INFO, "MCP server configured"))
        if cfg.get("command"):
            findings.append(Finding(
                loc, "gateway:subprocess", _HIGH,
                f"spawns a subprocess: command={cfg.get('command')!r}",
            ))
        env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
        secret_keys = [k for k in env if any(h in str(k).upper() for h in _SECRET_HINT)]
        if secret_keys:
            findings.append(Finding(
                loc, "gateway:egress+secrets", _HIGH,
                f"passes secret-looking env to the server: {secret_keys}",
            ))
        if cfg.get("url"):
            findings.append(Finding(loc, "gateway:egress", _INFO, f"network endpoint: {cfg.get('url')}"))
    return findings


# ── #1892: control_ir / phase-ops gateway analysis ──────────────────────────
# Deep static analysis of a skill's PHASE OPS (vs the .py scan in _gateway_skill):
# ``allowed_ops`` is the phase's op-KIND capability GRANT (statically declared);
# a preprocessor step carries a LITERAL op with literal args. Runtime LLM-emitted
# op args are out of static scope — the GRANT is the auditable gateway-exposure
# (the OpenClaw audit-* analog flags capability, not runtime values).
#
# Op-kind → gateway category (single-source). Severities lead-decided (#1892):
#   sandboxed_exec GRANT → MED (common + legit build/test grants → visibility,
#     no CI-block); a LITERAL preprocessor sandboxed_exec → HIGH (hard-coded exec
#     is materially riskier); network GRANT → egress INFO, + secret access →
#     egress+secrets HIGH; out-of-zone LITERAL preprocessor file path → broad-FS
#     MED. (Severity policy is config-tightenable in a later pass.)
# The map's completeness vs OP_PURITY world/side_effect is guarded by a test
# (test_audit_skill_ops_1892), not a runtime finding — avoids per-skill noise.
_EXEC_OPS = frozenset({"sandboxed_exec"})
_EGRESS_OPS = frozenset({"web_fetch", "web_search", "mcp", "call_mcp_tool"})
_SECRET_OPS = frozenset({"recall"})  # memory/secret retrieval (#1892 Q2)
_FS_PATH_OPS = frozenset({
    "read_file", "write_file", "edit_file", "delete_file", "glob_files",
    "grep_files", "file",
})


def _resolve_coarse(grants: set[str]) -> set[str]:
    """Expand coarse op families (``allowed_ops:[mcp]`` → its fine kinds) so the
    category match sees the real granted capability."""
    from reyn.core.op_runtime.registry import COARSE_TO_FINE
    out = set(grants)
    for g in grants:
        out |= set(COARSE_TO_FINE.get(g, frozenset()))
    return out


def _skill_reads_secrets(skill_dir: Path, grants: set[str]) -> bool:
    """#1892 Q2: a skill can read secrets if it GRANTS a memory/secret-retrieval
    op (``recall``) OR declares a secret-looking permission in skill.md."""
    if grants & _SECRET_OPS:
        return True
    from reyn.core.compiler.parser import parse_skill
    try:
        perms = parse_skill(skill_dir / "skill.md").permissions or {}
    except Exception:
        return False
    return any("secret" in str(k).lower() for k in perms)


def _is_out_of_zone(path: object) -> bool:
    """#1892 Q3: a literal preprocessor file-op path is out-of-zone if it is
    absolute or escapes upward (``..``). The runtime sandbox read/write allowlist
    is the dynamic guard; statically we flag a hard-coded path that leaves the
    skill/workspace-relative zone."""
    if not isinstance(path, str) or not path:
        return False
    from pathlib import PurePosixPath
    return os.path.isabs(path) or ".." in PurePosixPath(path).parts


def _gateway_skill_ops(skill_dir: Path) -> list[Finding]:
    """Rule 3 (#1892): static gateway analysis of a skill's phase ops — the
    op-KIND capability grants (``allowed_ops``) + literal preprocessor ops.
    Never executes (reads the parsed IR only)."""
    from reyn.core.compiler.parser import parse_phase

    parsed: list[tuple[Path, object]] = []
    for pf in sorted(skill_dir.glob("phases/*.md")):
        try:
            parsed.append((pf, parse_phase(pf)))
        except Exception:
            continue
    if not parsed:
        return []

    # #1892 Q2: secret access is a skill-wide signal (union of all phases' grants).
    union = _resolve_coarse({op for _, pd in parsed for op in (pd.allowed_ops or [])})
    reads_secrets = _skill_reads_secrets(skill_dir, union)

    findings: list[Finding] = []
    for pf, pd in parsed:
        loc = f"{skill_dir.name}/{pf.relative_to(skill_dir).as_posix()}"
        grants = _resolve_coarse(set(pd.allowed_ops or []))
        # (a) sandboxed_exec GRANT → MED capability (HIGH only for a literal exec, (c)).
        if grants & _EXEC_OPS:
            findings.append(Finding(
                loc, "gateway:exec-capability", _MED,
                "phase grants sandboxed_exec (op-layer subprocess capability)",
            ))
        # (b) network GRANT → egress INFO; + secret access → egress+secrets HIGH.
        net = sorted(grants & _EGRESS_OPS)
        if net:
            if reads_secrets:
                findings.append(Finding(
                    loc, "gateway:egress+secrets", _HIGH,
                    f"network grant {net} co-occurs with secret access "
                    f"(recall / secret-permission) — exfiltration risk",
                ))
            else:
                findings.append(Finding(
                    loc, "gateway:egress", _INFO, f"phase grants network op(s): {net}",
                ))
        # (c) preprocessor LITERAL ops (literal args ARE statically knowable).
        for step in (pd.preprocessor or []):
            op = step.get("op") if isinstance(step, dict) else None
            if not isinstance(op, dict):
                continue
            kind = op.get("kind")
            if kind in _EXEC_OPS:
                findings.append(Finding(
                    loc, "gateway:sandboxed-exec", _HIGH,
                    f"preprocessor ships a literal sandboxed_exec (argv={op.get('argv')!r})",
                ))
            elif kind in _FS_PATH_OPS:
                for path in (op.get("path"), op.get("dest_path")):
                    if _is_out_of_zone(path):
                        findings.append(Finding(
                            loc, "gateway:broad-fs", _MED,
                            f"preprocessor {kind} uses an out-of-zone literal path: {path!r}",
                        ))
    return findings


def _collect(only: str | None) -> list[Finding]:
    findings: list[Finding] = []
    for d in _skill_dirs(only):
        findings.extend(_scan_text_files(d))
        findings.extend(_gateway_skill(d))
        findings.extend(_gateway_skill_ops(d))  # #1892: deep phase-ops gateway
    findings.extend(_secrets_perm())
    if only is None:
        findings.extend(_gateway_mcp())
    return findings


def run(args: argparse.Namespace) -> None:
    findings = _collect(args.skill)
    high = [f for f in findings if f.severity == _HIGH]

    if args.json:
        print(json.dumps([asdict(f) for f in findings], ensure_ascii=False, indent=2))
    else:
        if not findings:
            print("reyn audit: no findings.")
        else:
            for f in findings:
                print(f"  [{f.severity:4}] {f.rule:24} {f.location}: {f.detail}")
            print(
                f"\nreyn audit: {len(findings)} finding(s) — "
                f"{len(high)} HIGH, "
                f"{sum(1 for f in findings if f.severity == _MED)} MED, "
                f"{sum(1 for f in findings if f.severity == _INFO)} INFO"
            )

    # Exit non-zero ONLY on a block-severity (HIGH) finding (CI-usable).
    if high:
        raise SystemExit(1)
