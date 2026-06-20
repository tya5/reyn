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


def _collect(only: str | None) -> list[Finding]:
    findings: list[Finding] = []
    for d in _skill_dirs(only):
        findings.extend(_scan_text_files(d))
        findings.extend(_gateway_skill(d))
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
