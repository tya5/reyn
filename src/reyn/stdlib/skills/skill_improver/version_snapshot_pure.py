"""Pure parser for self_improvement subset of reyn.yaml (R-PURE-MODE Wave 3b).

Mode: safe — imports only PURE_STDLIB_ALLOWLIST modules (re, typing).
Handles only the 2-field flat subset we currently need:

  self_improvement:
    on_propose: ask_user | auto | disabled
    max_versions: 10

If reyn.yaml's self_improvement schema grows beyond these flat fields,
this parser must be upgraded (= add YAML parsing via unsafe step or a new
config-aware run_op).

Honest scope: regex state machine over raw YAML text. Works because the
section has a predictable shape (flat key-value, no nesting, no YAML
sequences). Defaults mirror SelfImprovementConfig dataclass exactly:
  on_propose  → "ask_user"
  max_versions → 10

Do NOT add `from __future__ import annotations` — keep module-level
imports minimal so the function is importable without a full reyn install.
"""
import re
from typing import Any


def parse_on_propose_config_minimal(artifact: dict) -> dict:
    """Pure regex parser for self_improvement.on_propose / max_versions.

    Receives data._reyn_yaml_text (= raw reyn.yaml content written by a
    preceding file_read run_op step).  Extracts the two fields if present;
    falls back to defaults if absent or if reyn.yaml was missing (on_error:
    skip on the file_read step leaves _reyn_yaml_text absent).

    Returns:
        {"on_propose": str, "max_versions": int}

    Defaults match SelfImprovementConfig exactly:
        on_propose   = "ask_user"
        max_versions = 10
    """
    data: Any = artifact.get("data") or {}
    raw_text: Any = data.get("_reyn_yaml_text") if isinstance(data, dict) else None
    text: str = raw_text if isinstance(raw_text, str) else ""

    # Defaults from SelfImprovementConfig dataclass
    on_propose = "ask_user"
    max_versions = 10

    # State machine: scan for `self_improvement:` then read indented keys.
    # Block ends at the first non-indented, non-empty, non-comment line after
    # the header, or at EOF.
    in_block = False
    for line in text.splitlines():
        stripped = line.rstrip()

        # Skip blank lines and comments unconditionally (inside or outside block)
        if not stripped or stripped.lstrip().startswith("#"):
            continue

        # Detect the self_improvement: header (must be at column 0, no indent)
        if re.match(r"^self_improvement:\s*$", stripped):
            in_block = True
            continue

        # A non-indented non-empty line after the header ends the block
        if in_block and stripped[0] not in (" ", "\t"):
            in_block = False
            # Do NOT continue — this line belongs to another top-level key;
            # but since we already left the block we do not need to process it.

        if not in_block:
            continue

        # Parse on_propose
        m = re.match(r"[ \t]+on_propose:\s*(\S+)", line)
        if m:
            on_propose = m.group(1)
            continue

        # Parse max_versions (integer only)
        m = re.match(r"[ \t]+max_versions:\s*(\d+)", line)
        if m:
            max_versions = int(m.group(1))

    return {"on_propose": on_propose, "max_versions": max_versions}
