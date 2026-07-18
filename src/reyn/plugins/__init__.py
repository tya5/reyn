"""reyn.plugins — the plugin model substrate (ADR 0064).

P1 Foundation slice (#3067): the typed ``.reyn-plugin/plugin.json`` manifest
schema (``manifest.py``), the ``${REYN_*}`` / ``${CLAUDE_*}`` token-expansion
layer (``tokens.py``), and the ``kind``-precedence ordering for name
collisions across plugin sources (``source.py``).

Install machinery, permission gates, and the LLM/CLI/slash surfaces that
consume this substrate are out of scope here (ADR 0064 §3.2/§3.9/§3.10 —
P2+).
"""
from __future__ import annotations
