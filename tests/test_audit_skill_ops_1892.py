"""Tier 2: reyn audit — deep skill-ops gateway analysis (#1892).

Static analysis of a skill's phase ops: ``allowed_ops`` (the op-KIND capability
GRANT) + literal preprocessor ops. Real tmp skill fixtures parsed by the real
compiler parser; the analyzer never executes skill code. No mocks.

Severities (lead-decided #1892): sandboxed_exec grant → MED, literal preprocessor
exec → HIGH, network grant → egress INFO (+ secret access → HIGH), out-of-zone
literal preprocessor file path → broad-FS MED. Plus a completeness guard: every
world/side_effect op is in the category map or the acknowledged-benign set, so a
new gateway-relevant op fails here until triaged.
"""
from __future__ import annotations

from reyn.interfaces.cli.commands.audit import (
    _EGRESS_OPS,
    _EXEC_OPS,
    _FS_PATH_OPS,
    _SECRET_OPS,
    _gateway_skill_ops,
)


def _phase_md(name: str, allowed_ops=None, preprocessor: str = "") -> str:
    """Build a real phase.md as clean top-level lines (no dedent — the
    preprocessor block is appended at column 0 so YAML indentation is valid)."""
    lines = [
        "---", "type: phase", f"name: {name}",
        "input: {type: object, properties: {}}",
    ]
    if allowed_ops:
        lines.append("allowed_ops: [%s]" % ", ".join(allowed_ops))
    if preprocessor:
        lines.append(preprocessor)
    lines += ["---", "do work", ""]
    return "\n".join(lines)


def _skill(tmp_path, name: str, phases: dict[str, str]):
    """Build a real skill dir (skill.md + phases/<n>.md) parsed by the real parser."""
    d = tmp_path / name
    (d / "phases").mkdir(parents=True)
    (d / "skill.md").write_text(f"---\ntype: skill\nname: {name}\n---\nx\n", encoding="utf-8")
    for pname, fm in phases.items():
        (d / "phases" / f"{pname}.md").write_text(fm, encoding="utf-8")
    return d


def _rules(findings) -> set[tuple[str, str]]:
    return {(f.rule, f.severity) for f in findings}


def _runop(op_inline: str) -> str:
    return f"preprocessor:\n  - type: run_op\n    op: {{{op_inline}}}\n    into: data._x"


# ── grant signals (allowed_ops) ──────────────────────────────────────────────


def test_sandboxed_exec_grant_is_med(tmp_path):
    """Tier 2: a phase GRANTING sandboxed_exec → MED exec-capability (NOT HIGH —
    legit build/test skills; security-UX balance keeps CI-block off the grant)."""
    d = _skill(tmp_path, "s", {"act": _phase_md("act", allowed_ops=["sandboxed_exec"])})
    assert ("gateway:exec-capability", "MED") in _rules(_gateway_skill_ops(d))


def test_network_grant_is_egress_info(tmp_path):
    """Tier 2: a network grant with no secret access → egress INFO."""
    d = _skill(tmp_path, "s", {"act": _phase_md("act", allowed_ops=["web_fetch"])})
    assert ("gateway:egress", "INFO") in _rules(_gateway_skill_ops(d))


def test_network_plus_recall_is_egress_secrets_high(tmp_path):
    """Tier 2: a network grant co-occurring (skill-wide) with recall (secret
    access) → HIGH egress+secrets, NOT a plain egress INFO (upgraded)."""
    d = _skill(tmp_path, "s", {
        "fetch": _phase_md("fetch", allowed_ops=["web_fetch"]),
        "remember": _phase_md("remember", allowed_ops=["recall"]),
    })
    rules = _rules(_gateway_skill_ops(d))
    assert ("gateway:egress+secrets", "HIGH") in rules
    assert ("gateway:egress", "INFO") not in rules


def test_coarse_mcp_grant_is_egress(tmp_path):
    """Tier 2: a coarse ``mcp`` grant resolves (COARSE_TO_FINE) to call_mcp_tool →
    egress (the capability is seen through the coarse family)."""
    d = _skill(tmp_path, "s", {"act": _phase_md("act", allowed_ops=["mcp"])})
    assert any(r == "gateway:egress" for r, _ in _rules(_gateway_skill_ops(d)))


# ── literal preprocessor signals ─────────────────────────────────────────────


def test_literal_preprocessor_exec_is_high(tmp_path):
    """Tier 2: a LITERAL sandboxed_exec in a preprocessor → HIGH (a hard-coded
    exec is materially riskier than a mere grant)."""
    pre = _runop("kind: sandboxed_exec, argv: [echo, hi]")
    d = _skill(tmp_path, "s", {"act": _phase_md("act", allowed_ops=["sandboxed_exec"], preprocessor=pre)})
    assert ("gateway:sandboxed-exec", "HIGH") in _rules(_gateway_skill_ops(d))


def test_out_of_zone_absolute_path_is_broad_fs_med(tmp_path):
    """Tier 2: a literal preprocessor file op with an ABSOLUTE path → broad-FS MED."""
    pre = _runop("kind: write_file, path: /etc/evil")
    d = _skill(tmp_path, "s", {"act": _phase_md("act", allowed_ops=["write_file"], preprocessor=pre)})
    assert ("gateway:broad-fs", "MED") in _rules(_gateway_skill_ops(d))


def test_out_of_zone_traversal_path_is_broad_fs_med(tmp_path):
    """Tier 2: a literal preprocessor file op with a ``..`` traversal path → MED."""
    pre = _runop("kind: edit_file, path: ../../etc/passwd")
    d = _skill(tmp_path, "s", {"act": _phase_md("act", allowed_ops=["edit_file"], preprocessor=pre)})
    assert ("gateway:broad-fs", "MED") in _rules(_gateway_skill_ops(d))


def test_in_zone_relative_path_not_flagged(tmp_path):
    """Tier 2: a relative in-zone preprocessor path is NOT flagged (no false pos)."""
    pre = _runop("kind: write_file, path: out/result.txt")
    d = _skill(tmp_path, "s", {"act": _phase_md("act", allowed_ops=["write_file"], preprocessor=pre)})
    assert not any(f.rule == "gateway:broad-fs" for f in _gateway_skill_ops(d))


def test_benign_skill_has_no_gateway_ops_findings(tmp_path):
    """Tier 2: a skill with only read/grep ops + no exec/network/out-of-zone →
    no gateway-ops findings (no over-flagging the common case)."""
    d = _skill(tmp_path, "s", {"act": _phase_md("act", allowed_ops=["read_file", "grep_files"])})
    assert _gateway_skill_ops(d) == []


# ── completeness guard (vs OP_PURITY) ────────────────────────────────────────


def test_category_map_complete_vs_op_purity():
    """Tier 2: every world/side_effect op kind is classified in the gateway
    category map OR the acknowledged-benign set — a new gateway-relevant op added
    to OP_PURITY fails here until triaged (completeness-by-construction, the
    dev-time hint; no per-skill runtime noise)."""
    from reyn.core.op_runtime.registry import OP_PURITY, OpPurity

    gateway_relevant = {k for k, v in OP_PURITY.items()
                        if v in (OpPurity.world, OpPurity.side_effect)}
    classified = _EXEC_OPS | _EGRESS_OPS | _SECRET_OPS | _FS_PATH_OPS
    # Acknowledged: world/side_effect ops that are NOT a phase-ops gateway concern
    # (internal RAG/read, name resolve, task mgmt, UI, plugin lifecycle — the last
    # covered by _gateway_mcp). A new op in neither set → triage.
    acknowledged = {
        "index_query", "index_drop", "skill_resolve", "ask_user",
        "mcp_install", "mcp_drop_server",
        "task.get", "task.list", "task.create", "task.update_status",
        "task.add_dependency", "task.remove_dependency", "task.repoint_dependency",
        "task.abort", "task.heartbeat", "task.register_unblock_predicate", "task.comment",
    }
    uncovered = gateway_relevant - classified - acknowledged
    assert not uncovered, (
        f"world/side_effect op(s) unclassified for the gateway audit: {sorted(uncovered)} "
        f"— add to the category map (_EXEC/_EGRESS/_SECRET/_FS_PATH_OPS) or the acknowledged set"
    )
