"""S7 driver: Memory inline regression check (batch 17, ADR-0033 Phase 1).

Goal: verify that inline memory behavior still works after ADR-0033 added
the "Indexed sources" section to the router system prompt.

Strategy:
  - Use the sandbox root as workspace (reyn.yaml + reyn.local.yaml already configure LiteLLM proxy)
  - Create a temporary agent "s7_dogfood" with seeded memory files
  - Run N=3 chat sessions via subprocess (reyn chat --cui s7_dogfood)
  - Read events log to confirm recall tool was NOT called
  - Verify LLM reply references seeded memory content
  - Clean up afterward

Observations:
  1. Build system prompt programmatically → inspect "## Memory" + "## Indexed sources" structure.
  2. Run N=3 chat turns via subprocess (reyn chat --cui s7_dogfood).
  3. Read events log to confirm recall tool was NOT called.
  4. Verify LLM reply references seeded memory content.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────────
REYN_ROOT = Path(__file__).parent.parent
VENV = REYN_ROOT / "venv"
PYTHON = VENV / "bin" / "python"
REYN_BIN = VENV / "bin" / "reyn"

WORKSPACE = REYN_ROOT  # Use sandbox root — has LiteLLM proxy config
AGENT_NAME = "s7_dogfood"

# ── memory seed content ────────────────────────────────────────────────────────

FEEDBACK_OBSERVATION = """---
name: Feedback: Observation before speculation
description: Never accumulate hypotheses before establishing observation infra
type: feedback
---

When debugging LLM behavior, always set up observation infrastructure (trace dump, event log)
before speculating about root cause. Batches 9-10 showed that 4 hypotheses were raised,
and observation corrected 1.5 of them — pure speculation is error-prone. The REYN_LLM_TRACE_DUMP
env var plus dogfood_trace plus llm_replay are the four primary observation tools.
"""

FEEDBACK_SPLIT = """---
name: Feedback: Deterministic/Non-deterministic split
description: Do not delegate deterministic operations to the LLM
type: feedback
---

Operations that can be computed deterministically from inputs should NOT be delegated to the LLM.
The G2 fix (copy_to_work LLM-driven to preprocessor) demonstrated this principle. Three-question
checklist for skill design:
1. Is the output a pure function of the input?
2. What is the decision step?
3. Is anything except the decision delegated to the LLM?

This principle is a core consequence of P3 (OS is the runtime engine) and P5 (workspace as SSoT).
The LLM should only decide, not compute deterministic values.
"""

PROJECT_VISION = """---
name: Project: Vision
description: Reyn is designed for Japanese enterprises with high constraints
type: project
---

Reyn targets Japanese enterprise contexts with high compliance and audit requirements.
The design prioritizes predictability over autonomy: every run is replayable from
an append-only event log, every decision goes through a typed contract. This is
a deliberate trade-off — the LLM makes decisions within tightly constrained contexts,
not open-ended planning. This is reflected in the P1-P8 architecture principles and
the CLAUDE.md rules.
"""

MEMORY_INDEX_CONTENT = """- [Feedback: Observation before speculation](feedback_observation.md) — Never accumulate hypotheses before establishing observation infra
- [Feedback: Deterministic/Non-deterministic split](feedback_split.md) — Do not delegate deterministic operations to the LLM
- [Project: Vision](project_vision.md) — Reyn is designed for Japanese enterprises with high constraints
"""


def setup_agent_memory(agent_dir: Path) -> None:
    """Seed memory files into the agent's shared memory directory.

    We seed into the shared memory dir (.reyn/memory/), not agent-scoped.
    This mirrors how real users store memories.
    """
    # Use shared memory dir
    shared_mem_dir = WORKSPACE / ".reyn" / "memory"
    shared_mem_dir.mkdir(parents=True, exist_ok=True)

    # Save backup of existing MEMORY.md if present
    existing_index = shared_mem_dir / "MEMORY.md"
    backup_index = shared_mem_dir / "MEMORY.md.s7_backup"
    if existing_index.exists():
        shutil.copy2(str(existing_index), str(backup_index))
        print(f"  [backup] existing MEMORY.md backed up to {backup_index}")

    # Also check for existing memory files that might conflict
    for slug in ["feedback_observation", "feedback_split", "project_vision"]:
        f = shared_mem_dir / f"{slug}.md"
        backup = shared_mem_dir / f"{slug}.md.s7_backup"
        if f.exists():
            shutil.copy2(str(f), str(backup))

    # Write seed files
    (shared_mem_dir / "feedback_observation.md").write_text(FEEDBACK_OBSERVATION, encoding="utf-8")
    (shared_mem_dir / "feedback_split.md").write_text(FEEDBACK_SPLIT, encoding="utf-8")
    (shared_mem_dir / "project_vision.md").write_text(PROJECT_VISION, encoding="utf-8")

    # Rebuild MEMORY.md to include seeded entries
    # Use reyn memory's rewrite_index
    rebuild_script = f"""
import sys
sys.path.insert(0, '{REYN_ROOT}/src')
from pathlib import Path
from reyn.memory.memory import rewrite_index
rewrite_index(Path('{shared_mem_dir}'))
print("index rebuilt")
"""
    result = subprocess.run([str(PYTHON), "-c", rebuild_script], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        # Fallback: manually write MEMORY.md
        print(f"  [warn] rewrite_index failed: {result.stderr[:200]}, writing manually")
        (shared_mem_dir / "MEMORY.md").write_text(
            "# Memory Index\n\n" + MEMORY_INDEX_CONTENT,
            encoding="utf-8"
        )
    else:
        print(f"  [seed] {result.stdout.strip()}")

    print(f"  [seed] memory files written to {shared_mem_dir}")


def restore_memory(original_had_index: bool) -> None:
    """Restore memory dir to pre-test state."""
    shared_mem_dir = WORKSPACE / ".reyn" / "memory"
    # Remove seeded test files
    for slug in ["feedback_observation", "feedback_split", "project_vision"]:
        f = shared_mem_dir / f"{slug}.md"
        backup = shared_mem_dir / f"{slug}.md.s7_backup"
        if backup.exists():
            shutil.move(str(backup), str(f))
        elif f.exists():
            f.unlink()

    # Restore MEMORY.md
    backup_index = shared_mem_dir / "MEMORY.md.s7_backup"
    if backup_index.exists():
        shutil.move(str(backup_index), str(shared_mem_dir / "MEMORY.md"))
    else:
        # Rebuild from whatever remains
        rebuild_script = f"""
import sys
sys.path.insert(0, '{REYN_ROOT}/src')
from pathlib import Path
from reyn.memory.memory import rewrite_index
rewrite_index(Path('{shared_mem_dir}'))
"""
        subprocess.run([str(PYTHON), "-c", rebuild_script], capture_output=True, timeout=15)
    print("  [restore] memory files restored")


def inspect_system_prompt(agent_dir: Path) -> dict:
    """Build system prompt programmatically and return section analysis."""
    script = f"""
import sys
sys.path.insert(0, '{REYN_ROOT}/src')
import asyncio
import os
os.chdir('{WORKSPACE}')
from pathlib import Path
from reyn.chat.router_system_prompt import build_system_prompt
from reyn.chat.session import _merge_memory_indexes
from reyn.index.source_manifest import get_source_manifest

async def main():
    # Build memory index the same way ChatSession does
    shared_path = Path('.reyn/memory/MEMORY.md')
    agent_path = Path('.reyn/agents/{AGENT_NAME}/memory/MEMORY.md')
    memory_index = _merge_memory_indexes(
        shared_path=shared_path,
        agent_path=agent_path,
        agent_name='{AGENT_NAME}',
    )

    # Build indexed sources section (should be 0 available — no sources.yaml in workspace)
    manifest = get_source_manifest(Path.cwd())
    indexed_sources = await manifest.format_for_prompt()

    # Build system prompt
    sp = build_system_prompt(
        agent_name='{AGENT_NAME}',
        agent_role='dogfood test assistant',
        available_skills=[],
        available_agents=[],
        memory_index=memory_index,
        indexed_sources_section=indexed_sources,
    )
    print('=== SYSTEM_PROMPT_START ===')
    print(sp)
    print('=== SYSTEM_PROMPT_END ===')

asyncio.run(main())
"""
    result = subprocess.run(
        [str(PYTHON), "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(WORKSPACE),
    )
    if result.returncode != 0:
        print(f"  [ERROR] system prompt inspection failed:\n{result.stderr[:500]}")
        return {"error": result.stderr[:500]}

    sp_output = result.stdout
    start = sp_output.find("=== SYSTEM_PROMPT_START ===")
    end = sp_output.find("=== SYSTEM_PROMPT_END ===")
    if start == -1 or end == -1:
        return {"error": "markers not found", "raw": sp_output[:200]}
    sp = sp_output[start + len("=== SYSTEM_PROMPT_START ==="):end].strip()

    analysis = {
        "has_memory_section": "## Memory" in sp,
        "has_indexed_sources_section": "## Indexed sources" in sp,
        "indexed_sources_0_available": "## Indexed sources (0 available)" in sp,
        "memory_section_before_indexed": False,
        "memory_entries_found": [],
        "system_prompt_length": len(sp),
        "memory_section_excerpt": "",
        "indexed_sources_excerpt": "",
    }

    mem_pos = sp.find("## Memory")
    idx_pos = sp.find("## Indexed sources")
    if mem_pos != -1 and idx_pos != -1:
        analysis["memory_section_before_indexed"] = mem_pos < idx_pos

    for line in sp.splitlines():
        if any(slug in line for slug in ["feedback_observation", "feedback_split", "project_vision"]):
            analysis["memory_entries_found"].append(line.strip())

    if mem_pos != -1:
        end_mem = idx_pos if idx_pos > mem_pos else mem_pos + 500
        analysis["memory_section_excerpt"] = sp[mem_pos:end_mem].strip()

    if idx_pos != -1:
        analysis["indexed_sources_excerpt"] = sp[idx_pos:idx_pos + 500].strip()

    return analysis


def clean_agent_state(agent_dir: Path) -> None:
    """Clean agent state for fresh run (keeping memory untouched)."""
    # Remove events + state but keep memory
    for subdir in ["events", "state"]:
        d = agent_dir / subdir
        if d.exists():
            shutil.rmtree(d)
    # Remove history.jsonl (B16-S1-1 lesson)
    h = agent_dir / "history.jsonl"
    if h.exists():
        h.unlink()
    print("  [clean] agent state cleaned for fresh run")


def run_chat_session(agent_dir: Path, run_id: int, prompt: str) -> dict:
    """Run a single chat session and return observations."""
    print(f"\n--- Run {run_id} ---")

    clean_agent_state(agent_dir)

    start = time.time()
    result = subprocess.run(
        [str(REYN_BIN), "chat", "--cui", AGENT_NAME],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(WORKSPACE),
        env={**os.environ, "PYTHONPATH": str(REYN_ROOT / "src")},
    )
    elapsed = time.time() - start

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    print(f"  returncode: {result.returncode}")
    print(f"  elapsed: {elapsed:.1f}s")
    print(f"  stdout len: {len(stdout)}")

    # Check events log for tool calls
    events_dir = agent_dir / "events"
    tool_calls = []
    recall_calls = []
    if events_dir.exists():
        for ef in sorted(events_dir.glob("*.jsonl")):
            for line in ef.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                    ev_type = ev.get("type", "")
                    if ev_type in ("tool_call", "router_tool_call"):
                        tool_name = ev.get("name", ev.get("tool_name", ""))
                        tool_calls.append(tool_name)
                        if tool_name == "recall":
                            recall_calls.append(ev)
                except Exception:
                    pass

    # Extract actual LLM reply from stdout
    # The CUI output format: banner + "[…] thinking…" + actual reply
    # Strip ANSI codes and banner, look for actual text content
    import re
    # Remove ANSI escape codes
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean_stdout = ansi_escape.sub('', stdout)

    # Extract content after the thinking indicator
    reply_text = ""
    if "[…] thinking…" in clean_stdout:
        after_thinking = clean_stdout.split("[…] thinking…", 1)[1]
        # Strip the cost line at end
        lines = after_thinking.splitlines()
        content_lines = [l for l in lines if not l.startswith("cost")]
        reply_text = "\n".join(content_lines).strip()
    elif "cost" in clean_stdout:
        # Just show everything after the banner
        parts = clean_stdout.split("Ctrl-D to exit", 1)
        if len(parts) > 1:
            reply_text = parts[1].strip()

    # Check if LLM reply references memory content keywords
    reply_lower = reply_text.lower()
    memory_keywords = [
        "deterministic", "non-deterministic", "nondeterministic",
        "delegate", "llm", "checklist", "preprocessor",
        "p3", "p5", "pure function", "決定論", "委ね",
    ]
    reply_references_memory = any(kw in reply_lower for kw in memory_keywords)

    print(f"  tool_calls: {tool_calls}")
    print(f"  recall_invoked: {len(recall_calls) > 0}")
    print(f"  reply_references_memory: {reply_references_memory}")
    print(f"  reply_text (truncated): {reply_text[:300] if reply_text else '(empty)'}")

    # If the LLM returned an error or no reply, mark as failed
    llm_error = "LiteLLM" in stdout or "error" in stderr.lower() or not reply_text.strip()
    if llm_error and result.returncode == 0:
        # Check events for router_reply event to see actual reply
        if events_dir.exists():
            for ef in sorted(events_dir.glob("*.jsonl")):
                for line in ef.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                        if ev.get("type") in ("router_reply", "message_added") and ev.get("role") == "assistant":
                            text = ev.get("content", "") or ev.get("text", "")
                            if text:
                                reply_text = text
                                reply_lower = reply_text.lower()
                                reply_references_memory = any(kw in reply_lower for kw in memory_keywords)
                                print(f"  [events] found reply via events log: {reply_text[:200]}")
                    except Exception:
                        pass

    return {
        "run_id": run_id,
        "returncode": result.returncode,
        "elapsed_s": round(elapsed, 1),
        "stdout_len": len(stdout),
        "reply_text": reply_text[:500] if reply_text else "",
        "stderr_snippet": stderr[:300] if stderr else "",
        "tool_calls": tool_calls,
        "recall_calls": recall_calls,
        "recall_invoked": len(recall_calls) > 0,
        "reply_references_memory": reply_references_memory,
        "reply_text_found": bool(reply_text.strip()),
    }


def check_events_for_reply(agent_dir: Path) -> str:
    """Check events log for any router_reply or assistant message."""
    events_dir = agent_dir / "events"
    if not events_dir.exists():
        return ""
    for ef in sorted(events_dir.glob("*.jsonl")):
        for line in ef.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                if ev.get("type") in ("router_reply", "chat_reply", "assistant_message"):
                    return ev.get("text", ev.get("content", ""))[:500]
            except Exception:
                pass
    return ""


def determine_verdict(runs: list[dict], sp_sections: dict) -> tuple[str, str]:
    """Determine S7 verdict and reason."""
    n = len(runs)
    if n == 0:
        return "blocked", "no runs completed"

    # blocked: all runs crashed
    if all(r["returncode"] != 0 for r in runs):
        return "blocked", "all runs returned non-zero exit code"

    recall_count = sum(1 for r in runs if r["recall_invoked"])
    memory_used_count = sum(1 for r in runs if r["reply_references_memory"])

    # Check system prompt structure
    if not sp_sections.get("has_memory_section"):
        return "refuted", "Memory section missing from system prompt"
    if not sp_sections.get("has_indexed_sources_section"):
        return "refuted", "Indexed sources section missing from system prompt (ADR-0033 regression)"

    # refuted: recall called
    if recall_count > 0:
        return "refuted", f"recall tool invoked {recall_count}/{n} times (should be 0)"

    # System prompt structure OK, no recall - check LLM usage of memory
    if memory_used_count == 0:
        return "refuted", "LLM did not reference inline memory content in any reply (0/3)"

    if memory_used_count >= 2:
        return "verified", (
            f"recall=0/{n}, memory inline used {memory_used_count}/{n}, "
            f"both sections coexist in SP"
        )

    return "inconclusive", (
        f"recall=0/{n} (good), memory inline used {memory_used_count}/{n} (ambiguous)"
    )


def main():
    print("=== S7: Memory inline regression check (N=3) ===")
    print(f"REYN_ROOT: {REYN_ROOT}")
    print(f"WORKSPACE: {WORKSPACE}")
    print(f"AGENT: {AGENT_NAME}")

    agent_dir = WORKSPACE / ".reyn" / "agents" / AGENT_NAME

    # Step 1: Seed memory
    print("\n[Step 1] Seeding memory...")
    setup_agent_memory(agent_dir)

    try:
        # Step 2: Inspect system prompt structure
        print("\n[Step 2] Inspecting system prompt structure...")
        sp_sections = inspect_system_prompt(agent_dir)

        print(f"  has_memory_section: {sp_sections.get('has_memory_section')}")
        print(f"  has_indexed_sources_section: {sp_sections.get('has_indexed_sources_section')}")
        print(f"  indexed_sources_0_available: {sp_sections.get('indexed_sources_0_available')}")
        print(f"  memory_before_indexed: {sp_sections.get('memory_section_before_indexed')}")
        print(f"  memory_entries_found: {sp_sections.get('memory_entries_found')}")
        print(f"  sp_length: {sp_sections.get('system_prompt_length')}")

        if sp_sections.get("memory_section_excerpt"):
            print("\n--- Memory section (from SP) ---")
            print(sp_sections["memory_section_excerpt"][:600])
        if sp_sections.get("indexed_sources_excerpt"):
            print("\n--- Indexed sources section (from SP) ---")
            print(sp_sections["indexed_sources_excerpt"][:500])

        # Step 3: Run N=3 chat sessions
        PROMPT = "What feedback did the user give about deterministic / non-deterministic split?"
        print(f"\n[Step 3] Running N=3 chat sessions with prompt: {PROMPT!r}")

        agent_dir.mkdir(parents=True, exist_ok=True)
        runs = []
        for i in range(1, 4):
            run_result = run_chat_session(agent_dir, i, PROMPT)
            runs.append(run_result)

        # Step 4: Verdict
        verdict, reason = determine_verdict(runs, sp_sections)
        recall_total = sum(1 for r in runs if r["recall_invoked"])
        memory_used_total = sum(1 for r in runs if r["reply_references_memory"])

        print("\n=== RESULTS ===")
        print(f"verdict: {verdict}")
        print(f"reason: {reason}")
        print(f"recall invoked: {recall_total}/{len(runs)}")
        print(f"memory inline used: {memory_used_total}/{len(runs)}")
        print(f"both sections coexist: {sp_sections.get('has_memory_section') and sp_sections.get('has_indexed_sources_section')}")

        output = {
            "verdict": verdict,
            "reason": reason,
            "sp_sections": {k: v for k, v in sp_sections.items()
                           if k not in ("memory_section_excerpt", "indexed_sources_excerpt")},
            "memory_section_excerpt": sp_sections.get("memory_section_excerpt", ""),
            "indexed_sources_excerpt": sp_sections.get("indexed_sources_excerpt", ""),
            "runs": runs,
            "summary": {
                "recall_invoked_rate": f"{recall_total}/{len(runs)}",
                "memory_inline_used_rate": f"{memory_used_total}/{len(runs)}",
                "both_sections_present": sp_sections.get("has_memory_section") and sp_sections.get("has_indexed_sources_section"),
                "memory_before_indexed": sp_sections.get("memory_section_before_indexed"),
            }
        }

        print("\n=== JSON OUTPUT ===")
        print(json.dumps(output, indent=2, ensure_ascii=False))

        return output

    finally:
        # Always restore memory
        print("\n[Cleanup] Restoring memory...")
        restore_memory(original_had_index=True)
        # Clean up agent dir
        if agent_dir.exists():
            shutil.rmtree(agent_dir)
            print(f"  [clean] removed {agent_dir}")


if __name__ == "__main__":
    main()
