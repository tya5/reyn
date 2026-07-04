"""Reyn Pipeline control plane — the deterministic-DSL successor to the skill/task system.

This package holds the pipeline feature's pure, non-agentic building blocks (see
``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md``, resolution R1). The first
brick is ``expr``: the total expression evaluator used by ``transform.value`` / ``until`` /
``verify.condition`` / ``fold.init``.
"""
from __future__ import annotations
