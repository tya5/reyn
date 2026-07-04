"""`reyn audit` — static safety scan of MCP plugins + secrets + delegation (#1864, #1822 Part 3).

A STATIC, install-time / on-demand audit (distinct from the #1822 S1-S5 runtime
content-threat scan). It READS the MCP plugin config, the secrets file, and the
delegation topology and pattern-scans them — it never imports or executes code.
The OpenClaw ``audit-*`` analog.

Rules (lead-greenlit, #1864; #2081 S3 adds the delegation rule):
  1. **secrets permission** — ``~/.reyn/secrets.env`` is written chmod 600
     (security/secrets/store.py); flag any group/other-accessible deviation.
  2. **gateway exposure** — MCP plugin configs are read directly: a ``command``
     (subprocess spawn, HIGH), secret-looking ``env`` keys (+secrets, HIGH), a
     network ``url`` (egress, INFO), and every server is enumerated (INFO).
  3. **delegation-unsafe** (#2081 S3) — a delegate-REACHABLE topology role (inbound
     ``can_send`` target) whose bound capability_profile, or the ``_delegate.yaml``
     override, re-grants a dangerous class (re-delegation / exec = HIGH; memory-write /
     destructive-FS = MED); + an INFO posture nudge when ``delegation.capability_default
     =inherit`` while a topology permits delegation.

``reyn audit [--json]`` → findings; **exit non-zero only on a
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
    location: str   # where (agent/plugin + file)
    rule: str       # which rule fired
    severity: str   # HIGH / MED / INFO
    detail: str


def register(sub) -> None:
    p = sub.add_parser(
        "audit",
        help="Static safety scan of installed agents / plugins (code/config + secrets + gateway)",
    )
    p.add_argument("--json", action="store_true", help="Emit findings as JSON.")
    p.set_defaults(func=run)


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
def _gateway_delegation() -> list[Finding]:
    """Rule 4 (#2081 S3): delegation-unsafe capability. The audit flags, per dangerous
    CLASS (re-delegation / exec = HIGH; memory-write / destructive-FS = MED):

    - a delegate-REACHABLE topology role — a member with an inbound ``can_send`` edge,
      = a delegation target (the A2A request path is can_send-gated) — whose bound
      capability_profile PERMITS the class. Reachability-precise (OPT-A): an
      outbound-only role (e.g. a hierarchy's top coordinator) that legitimately holds
      ``delegate_to_agent`` is NOT a delegation target, so it is NOT flagged — avoiding
      a false HIGH (which would wrongly ``exit(1)`` / block a deploy).
    - the ``_delegate.yaml`` override permitting a class — it IS the global delegate
      floor, so a re-grant there applies to every unbound delegate (no reachability
      needed).
    - INFO posture: ``delegation.capability_default=inherit`` while a topology has any
      delegation edge — delegates inherit the spawner's full capability (a nudge, not
      a block; ``inherit`` is the default)."""
    from reyn.security.permissions.capability_profile import (
        DELEGATE_PROFILE_NAME,
        DELEGATION_AUDIT_CLASSES,
        load_capability_profile,
        profile_permits,
    )

    findings: list[Finding] = []
    root = Path.cwd()
    try:
        from reyn.config import load_config
        cap_default = load_config().delegation.capability_default
    except Exception as exc:  # never let config-load break the audit
        return [Finding("reyn.yaml delegation:", "gateway:delegation-unsafe", _INFO,
                        f"could not load delegation config: {exc}")]

    topologies = []
    topo_dir = root / ".reyn" / "topologies"
    if topo_dir.is_dir():
        from reyn.runtime.topology import Topology
        for p in sorted(topo_dir.glob("*.yaml")):
            try:
                topologies.append(Topology.load(p))
            except Exception:  # a malformed topology is surfaced by other paths
                continue

    # OPT-C: inherit + any delegation edge → a posture nudge (INFO, never a block).
    if cap_default == "inherit" and any(t.edges() for t in topologies):
        findings.append(Finding(
            "reyn.yaml delegation:", "gateway:delegation-unsafe", _INFO,
            "capability_default=inherit: delegated agents inherit the spawner's full "
            "capability (incl. re-delegation / exec / memory-write). Set "
            "delegation.capability_default=deny to apply the restrictive _delegate floor.",
        ))

    prof_dir = root / ".reyn" / "capability_profiles"

    def _scan(profile, loc: str) -> None:
        for cls, (sev, tools) in DELEGATION_AUDIT_CLASSES.items():
            permitted = sorted(t for t in tools if profile_permits(profile, t))
            if permitted:
                findings.append(Finding(
                    loc, "gateway:delegation-unsafe", sev,
                    f"permits {cls} for a delegate: {permitted}",
                ))

    # OPT-A: per-class re-grant scan of delegate-REACHABLE bound profiles.
    for topo in topologies:
        targets = {x for x in topo.members if any(topo.can_send(y, x) for y in topo.members)}
        for member in sorted(targets):
            pname = topo.profiles.get(member)
            if not pname:
                continue
            ppath = prof_dir / f"{pname}.yaml"
            if not ppath.is_file():
                continue
            try:
                _scan(load_capability_profile(ppath), f"topology {topo.name}/{member} (profile {pname})")
            except Exception:
                continue

    # the _delegate.yaml override is the GLOBAL delegate floor — scan it always.
    dpath = prof_dir / f"{DELEGATE_PROFILE_NAME}.yaml"
    if dpath.is_file():
        try:
            _scan(load_capability_profile(dpath), f"capability_profiles/{DELEGATE_PROFILE_NAME}.yaml")
        except Exception:
            pass

    return findings


def _collect() -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_secrets_perm())
    findings.extend(_gateway_mcp())
    findings.extend(_gateway_delegation())  # #2081 S3
    return findings


def run(args: argparse.Namespace) -> None:
    findings = _collect()
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
