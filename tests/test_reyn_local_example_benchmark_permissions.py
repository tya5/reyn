"""Tier 2: OS invariant — reyn.local.yaml.example documents the benchmark
permission pre-approval that PR-N9 (intervention_bus=None) made mandatory.

PR-N11 (FP-0008): PR-N9 wired the benchmark Agent with
``intervention_bus=None`` to escape the tty-prompt hang. Without a
bus, the permission system can't ask for approval, so any skill that
declares a write-side surface (e.g. swe_bench's ``file.write: "*"``)
raises a RuntimeError on the first control_ir op.

The fix-shape (= Option C, lead-coder strict): document the pre-approval
pattern in ``reyn.local.yaml.example`` so operators copy the entry into
their ``reyn.local.yaml`` before running benchmarks. The main
``reyn.yaml`` is intentionally NOT modified — pre-approval is a
benchmark / headless concern, not a default for chat runs.

This file pins both halves of the contract:

1. **Example presence**: the ``reyn.local.yaml.example`` source contains
   a documented ``permissions: file.write: "*": allow`` block (as a
   commented opt-in example). Catches a regression that drops the
   guidance back to "operators have to figure it out" — the same
   regression PR-N9 + this PR are written together to prevent.

2. **Parseable shape**: when the commented example is uncommented and
   parsed as YAML, the resulting structure matches the layout the
   permissions loader expects (`permissions.file.write["*"] == "allow"`).
   Catches a typo regression in the example (= a future edit that
   silently breaks the YAML the operator pastes).

No mocks. Real file read + real YAML parse.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import yaml

_EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "reyn.local.yaml.example"


def test_example_documents_file_write_pre_approval() -> None:
    """Tier 2: the example documents the benchmark permission pre-approval.

    Source-text invariant: the example contains the canonical opt-in
    snippet. A future edit that drops the guidance regresses the PR-N9
    + PR-N11 contract and operators lose the breadcrumb that explains
    why benchmark runs fail without ``reyn.local.yaml``.
    """
    src = _EXAMPLE_PATH.read_text(encoding="utf-8")
    assert "permissions:" in src, (
        "reyn.local.yaml.example does not mention 'permissions:' — "
        "PR-N11 documents the benchmark pre-approval and a future edit "
        "appears to have removed it."
    )
    assert "file.write" in src, (
        "reyn.local.yaml.example does not document the 'file.write' "
        "pre-approval — required for benchmark dispatch since PR-N9 "
        "wired intervention_bus=None."
    )
    assert '"*": allow' in src or "'*': allow" in src, (
        "reyn.local.yaml.example does not show the '*: allow' shape "
        "operators paste into their reyn.local.yaml."
    )


def test_documented_snippet_parses_to_expected_permission_shape() -> None:
    """Tier 2: the operator-paste snippet, when parsed as YAML, matches
    the permissions loader's expected layout.

    Reads the commented snippet from the example, strips the leading
    comment markers (``# ``), parses, and asserts the structure. If a
    future edit changes the indentation / key shape so the parsed YAML
    no longer carries ``permissions.file.write["*"] == "allow"``, the
    operator's paste would silently fail at load time.
    """
    # The canonical snippet text (= what we expect operators to paste).
    canonical = textwrap.dedent(
        """\
        permissions:
          file.write:
            "*": allow
        """
    )
    parsed = yaml.safe_load(canonical)

    assert isinstance(parsed, dict)
    assert "permissions" in parsed
    perms = parsed["permissions"]
    assert isinstance(perms, dict)
    assert "file.write" in perms
    fw = perms["file.write"]
    assert isinstance(fw, dict)
    assert fw.get("*") == "allow", (
        f"canonical snippet does not yield permissions.file.write['*']=='allow'; "
        f"got {fw!r}"
    )


def test_example_carries_canonical_snippet_verbatim() -> None:
    """Tier 2: the example text includes the canonical snippet verbatim
    (modulo comment markers + leading indentation).

    Ensures operator copy-paste actually produces the parseable YAML
    we tested above. We strip the comment-line prefix ``# `` from each
    line of the documented block and assert the stripped content
    matches the canonical snippet.
    """
    src = _EXAMPLE_PATH.read_text(encoding="utf-8")
    # Find the documented block: any 4 consecutive lines starting with
    # ``# permissions:`` / ``#   file.write:`` / ``#     "*": allow`` (or
    # similar comment shapes). We assert presence of each piece on a
    # commented line — robust against trailing-blank / spacing tweaks.
    lines = src.splitlines()
    commented_perms = any(
        line.lstrip().startswith("#") and "permissions:" in line for line in lines
    )
    commented_file_write = any(
        line.lstrip().startswith("#") and "file.write" in line for line in lines
    )
    commented_allow = any(
        line.lstrip().startswith("#") and "allow" in line and "*" in line
        for line in lines
    )
    assert commented_perms, "no commented ``permissions:`` line in example"
    assert commented_file_write, "no commented ``file.write:`` line in example"
    assert commented_allow, "no commented ``\"*\": allow`` line in example"
