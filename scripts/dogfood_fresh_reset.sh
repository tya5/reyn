#!/usr/bin/env bash
# dogfood_fresh_reset.sh — idempotent fresh-mode state reset for dogfood workers.
#
# Wipes the per-batch hot-list / WAL / plan-resume state so every scenario
# starts from DEFAULT_HOT_LIST_SEED with no carry-over.
#
# What this script wipes:
#   .reyn/state/wal.jsonl           — plan-resume substrate (if present)
#   .reyn/state/history.jsonl       — session-summary carry-over (if present)
#   .reyn/state/plans/              — stale decomposition artifacts (if present)
#   reyn/local/                     — workspace-local skills from prior skill_builder runs
#
# What this script does NOT wipe:
#   .reyn/events/                   — event log; do not wipe while reyn web is running
#   .reyn/agents/*/events           — per-agent event log; same constraint
#   .reyn/agents/*/history.jsonl    — per-agent conversation history; requires knowing
#                                     the agent name — callers must wipe this separately
#   .reyn/agents/*/action_usage.json — per-agent hot-list ledger; requires the agent name,
#                                     so the runner wipes it per-scenario (#2357). The old
#                                     .reyn/state/action_usage.jsonl path never existed (the
#                                     live ledger is per-agent) — wiping it here was a no-op.
#
# Rationale: §6.7 of docs/deep-dives/contributing/dogfood-discipline.md.
# Cross-batch V comparison is only valid when all batches start from the same
# deterministic state. This script enforces that baseline (B37 retro §3 F2 /
# B38 retro §6 evidence).
#
# Usage:
#   bash scripts/dogfood_fresh_reset.sh              # run from worktree root
#   bash scripts/dogfood_fresh_reset.sh /path/to/wt  # explicit worktree root
#
# Idempotent: safe to run even when the files are already absent.

set -euo pipefail

ROOT="${1:-$(pwd)}"

# Resolve to absolute path
ROOT="$(cd "$ROOT" && pwd)"

echo "[dogfood_fresh_reset] worktree: $ROOT"

_remove_file() {
    local path="$ROOT/$1"
    if [ -f "$path" ]; then
        rm -f "$path"
        echo "[dogfood_fresh_reset] removed file: $1"
    fi
}

_remove_dir() {
    local path="$ROOT/$1"
    if [ -d "$path" ]; then
        rm -rf "$path"
        echo "[dogfood_fresh_reset] removed dir:  $1"
    fi
}

# #2357: the hot-list ledger (the primary measurement confound, B37 F2 / B38 §6) is per-agent
# (.reyn/agents/<name>/action_usage.json) — this script has no agent name, so the runner wipes it
# per-scenario. The old `_remove_file ".reyn/state/action_usage.jsonl"` targeted a path that never
# existed (silent no-op) and is removed here.

# Plan-resume substrate
_remove_file ".reyn/state/wal.jsonl"

# Session-summary carry-over
_remove_file ".reyn/state/history.jsonl"

# Stale plan decomposition artifacts
_remove_dir ".reyn/state/plans"

# Workspace-local skills (B30-NEW-3: skill_builder writes here; bleeds into list_actions)
_remove_dir "reyn/local"

echo "[dogfood_fresh_reset] done — workspace is in fresh mode"
