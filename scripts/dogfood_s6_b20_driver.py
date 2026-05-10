#!/usr/bin/env python3
"""Dogfood batch 20 — S6 redesign: Multi-source recall (synthetic sources).

Batch 19 self-audit found the original S6 had a scenario design flaw —
prompt "How is recall implemented?" is a code-reading query that
naturally matches `reyn_src_read` description ("how does Reyn / how
does Reyn's X work?"), and indexed `reyn_docs` (concept docs) didn't
contain implementation details. The LLM's `reyn_src_read` choice was
correct routing, NOT an attractor.

Batch 20 redesigns S6 with **synthetic indexed sources** that:
1. Don't overlap with Reyn's actual repo (= reyn_src_read returns no
   useful answer for the prompt → that affordance is removed)
2. Have content split between concept-style and code-style chunks
   (= testing genuine multi-source recall behaviour)
3. Use a fictional topic ("quantum bridge protocol") so the LLM
   cannot fall back on training knowledge

Verdict criteria (= 原則 12 verdict false-attribution discipline):
- verified: recall invoked + sources field contains BOTH
  quantum_concepts AND quantum_code
- refuted_class_b_a1: recall invoked but only 1 source picked
  (= multi-source attractor hypothesis direct evidence)
- refuted_class_b_a2: recall NOT invoked at all (= different
  attractor, not multi-source related)
- inconclusive: driver / subprocess error
- blocked: structural pre-check fail (= recall not in catalog)
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

REYN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REYN_ROOT / "scripts"))

from dogfood_rag_helper import (  # noqa: E402
    make_chunks_for_seed,
    register_fake_embedding_provider,
    write_dogfood_reyn_yaml,
    write_index_directly,
)

WORKSPACE = Path("/tmp/reyn_s6_b20_ws")
TRACE_DIR = Path("/tmp/reyn_s6_b20")
BOOTSTRAP_DIR = Path("/tmp/reyn_s6_b20_bootstrap")
PROMPT = "How does the quantum bridge protocol work?"
N_RUNS = 3

# Synthetic concept-style chunks (= what would live in /docs)
CONCEPT_TEXTS = [
    "The Quantum Bridge Protocol (QBP) is a synchronisation mechanism "
    "for distributed quantum-state stores. It exposes two endpoints — "
    "an `entangler` for pairing remote registers and a "
    "`decoherence_buffer` for fault tolerance during transit.",
    "QBP guarantees that paired registers observe a consistent collapse "
    "ordering even under partial network partition. The protocol is "
    "designed for low-latency intra-region links (< 5 ms RTT) and is "
    "not recommended for inter-continental links.",
    "A typical QBP session begins with a three-way handshake: SYN, "
    "ENT-OFFER, ENT-ACK. After the handshake, both sides hold a "
    "Bell-pair reference and can exchange state-mutation messages "
    "freely until the bridge is torn down with a TEARDOWN frame.",
    "Decoherence buffering trades latency for reliability. When the "
    "buffer is enabled, every state mutation is replayed N times "
    "(default N=3) before being committed. Buffer drains on session "
    "close or after 200ms idle.",
    "Common use cases for QBP include: distributed photon counter "
    "telemetry, multi-site quantum key distribution coordination, and "
    "synchronisation of GHZ states across array detectors.",
    "QBP is layered on top of a generic message bus (e.g. NATS, "
    "RabbitMQ). The protocol does not assume reliable delivery — it "
    "uses the decoherence buffer to recover from message loss.",
]

# Synthetic code-style chunks (= what would live in /src)
CODE_TEXTS = [
    "class Entangler:\n"
    "    def __init__(self, partner_id: str, bell_basis: str = 'phi+'):\n"
    "        self.partner_id = partner_id; self.bell_basis = bell_basis\n"
    "    async def offer(self) -> EntOffer:\n"
    "        return EntOffer(basis=self.bell_basis, nonce=random_nonce())",

    "class DecoherenceBuffer:\n"
    "    def __init__(self, n_replays: int = 3, idle_drain_ms: int = 200):\n"
    "        self.n_replays = n_replays; self.queue = []\n"
    "    async def replay_and_commit(self, frame: Frame) -> None:\n"
    "        for _ in range(self.n_replays):\n"
    "            await self.transport.send(frame)",

    "async def bridge_handshake(local: Entangler, remote_addr: str) -> Bridge:\n"
    "    await transport.send(SynFrame(local_id=local.id))\n"
    "    offer = await transport.recv(timeout=5.0)\n"
    "    if offer.kind != 'ent_offer': raise ProtocolError('expected ENT-OFFER')\n"
    "    await transport.send(EntAck(nonce=offer.nonce))\n"
    "    return Bridge(local=local, remote=offer.partner_id)",

    "def teardown_bridge(bridge: Bridge) -> None:\n"
    "    bridge.buffer.drain_now()\n"
    "    bridge.transport.send(TeardownFrame(reason='session_end'))\n"
    "    bridge.state = BridgeState.CLOSED",

    "class BridgeState(Enum):\n"
    "    INIT = 'init'\n"
    "    HANDSHAKING = 'handshaking'\n"
    "    OPEN = 'open'\n"
    "    DRAINING = 'draining'\n"
    "    CLOSED = 'closed'",

    "REGISTRY: dict[str, Entangler] = {}\n"
    "def register_entangler(e: Entangler) -> None:\n"
    "    REGISTRY[e.partner_id] = e\n"
    "def get_entangler(partner_id: str) -> Entangler | None:\n"
    "    return REGISTRY.get(partner_id)",
]


def reset_workspace() -> None:
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True)


def clean_history() -> None:
    history = WORKSPACE / ".reyn" / "agents" / "default" / "history.jsonl"
    if history.exists():
        history.unlink()


async def seed_two_synthetic_sources() -> None:
    """Pre-seed quantum_concepts + quantum_code with FakeEmbeddingProvider.

    Sources are fictional (no overlap with Reyn repo) so reyn_src_read
    cannot answer the prompt; the only viable routing is `recall`.
    """
    register_fake_embedding_provider()
    concept_chunks = make_chunks_for_seed(
        CONCEPT_TEXTS, "docs/quantum_bridge.md", "md"
    )
    code_chunks = make_chunks_for_seed(
        CODE_TEXTS, "src/quantum/bridge.py", "py"
    )
    await write_index_directly(
        WORKSPACE,
        source="quantum_concepts",
        description=(
            "Conceptual documentation for the Quantum Bridge Protocol "
            "(QBP) — overview, handshake, decoherence buffering, use cases."
        ),
        path_glob="docs/quantum/**/*.md",
        chunks_data=concept_chunks,
    )
    await write_index_directly(
        WORKSPACE,
        source="quantum_code",
        description=(
            "Source code implementation of the Quantum Bridge Protocol "
            "(QBP) — Entangler, DecoherenceBuffer, handshake, teardown."
        ),
        path_glob="src/quantum/**/*.py",
        chunks_data=code_chunks,
    )


def setup_bootstrap() -> None:
    BOOTSTRAP_DIR.mkdir(parents=True, exist_ok=True)
    sc = BOOTSTRAP_DIR / "sitecustomize.py"
    helper_path = REYN_ROOT / "scripts"
    sc.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(helper_path)!r})\n"
        "try:\n"
        "    from dogfood_rag_helper import register_fake_embedding_provider\n"
        "    register_fake_embedding_provider()\n"
        "except Exception as e:\n"
        "    import sys as _s; print('[bootstrap] fake provider register failed:', e, file=_s.stderr)\n"
    )


def ensure_openai_key_in_env() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    try:
        out = subprocess.run(
            ["zsh", "-lic", "echo -n $OPENAI_API_KEY"],
            capture_output=True, text=True, timeout=10,
        )
        key = out.stdout.strip()
        if key:
            os.environ["OPENAI_API_KEY"] = key
    except Exception:
        pass


def setup_workspace() -> None:
    reset_workspace()
    setup_bootstrap()
    ensure_openai_key_in_env()
    write_dogfood_reyn_yaml(WORKSPACE)
    src_local = REYN_ROOT / "reyn.local.yaml"
    if src_local.exists():
        (WORKSPACE / "reyn.local.yaml").write_text(src_local.read_text())
    subprocess.run(
        ["reyn", "agent", "new", "default"],
        cwd=str(WORKSPACE),
        capture_output=True,
        timeout=30,
    )
    asyncio.run(seed_two_synthetic_sources())
    out = subprocess.run(
        ["reyn", "source", "list"],
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
        timeout=30,
    )
    print(f"[setup] source list output:\n{out.stdout}")


def run_chat_turn(run_id: int) -> dict:
    trace_file = TRACE_DIR / f"run_{run_id}.jsonl"
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    if trace_file.exists():
        trace_file.unlink()

    existing_pp = os.environ.get("PYTHONPATH", "")
    pp = str(BOOTSTRAP_DIR) + (os.pathsep + existing_pp if existing_pp else "")
    env = {
        **os.environ,
        "REYN_EMBEDDING_PROVIDER": "fake",
        "REYN_LLM_TRACE_DUMP": str(trace_file),
        "PYTHONPATH": pp,
    }

    start = time.time()
    result = subprocess.run(
        ["reyn", "chat", "--cui"],
        input=f"{PROMPT}\n",
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(WORKSPACE),
        env=env,
    )
    elapsed = time.time() - start

    recall_called = False
    recall_sources: list[str] | None = None
    all_tool_calls: list[dict] = []
    sp_indexed_section = ""
    catalog_has_recall = False

    if trace_file.exists():
        for line in trace_file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            kind = d.get("kind", "")
            if kind == "request":
                tools = d.get("tools") or []
                for t in tools:
                    fn = t.get("function") or {}
                    if fn.get("name") == "recall":
                        catalog_has_recall = True
                for m in d.get("messages", []):
                    if m.get("role") == "system":
                        c = str(m.get("content", ""))
                        idx = c.find("## Indexed sources")
                        if idx >= 0 and not sp_indexed_section:
                            sp_indexed_section = c[idx : idx + 600]
            elif kind == "response":
                tcs = d.get("tool_calls") or []
                for tc in tcs:
                    fn = tc.get("function") or {}
                    name = fn.get("name", "")
                    raw_args = fn.get("arguments", "")
                    try:
                        args = (
                            json.loads(raw_args)
                            if isinstance(raw_args, str)
                            else (raw_args or {})
                        )
                    except Exception:
                        args = {}
                    all_tool_calls.append({"name": name, "args": args})
                    if name == "recall":
                        recall_called = True
                        if recall_sources is None:
                            recall_sources = args.get("sources")

    return {
        "run_id": run_id,
        "elapsed": round(elapsed, 1),
        "returncode": result.returncode,
        "recall_called": recall_called,
        "recall_sources": recall_sources,
        "all_tool_calls": all_tool_calls,
        "sp_indexed_section": sp_indexed_section[:300],
        "catalog_has_recall": catalog_has_recall,
        "stderr": result.stderr[-400:],
        "stdout_tail": result.stdout[-300:],
    }


def assess_verdict(obs: dict) -> str:
    """Verdict per prelude §5 (= class B subdivide)."""
    if not obs["catalog_has_recall"]:
        return "blocked"
    if obs["returncode"] != 0 and not obs["recall_called"]:
        if "Traceback" in obs["stderr"]:
            return "inconclusive"
    if not obs["recall_called"]:
        return "refuted_class_b_a2"  # recall not invoked at all
    sources = obs["recall_sources"]
    if not isinstance(sources, list):
        return "refuted_class_b_a1"  # recall called but sources missing/empty
    has_concepts = "quantum_concepts" in sources
    has_code = "quantum_code" in sources
    if has_concepts and has_code:
        return "verified"
    return "refuted_class_b_a1"  # 1 source only


def main() -> None:
    print("=" * 60)
    print("S6 retest (Batch 20): Multi-source recall (synthetic sources)")
    print(f"Workspace: {WORKSPACE}")
    print(f"Trace dir: {TRACE_DIR}")
    print(f"Prompt: {PROMPT!r}")
    print("=" * 60)

    setup_workspace()

    results = []
    for run_id in range(1, N_RUNS + 1):
        print(f"\n[Run {run_id}/{N_RUNS}] Cleaning history, running chat...")
        clean_history()
        obs = run_chat_turn(run_id)
        verdict = assess_verdict(obs)
        obs["verdict"] = verdict
        print(f"  Elapsed: {obs['elapsed']}s  rc={obs['returncode']}")
        print(f"  catalog_has_recall: {obs['catalog_has_recall']}")
        print(f"  recall_called: {obs['recall_called']}")
        print(f"  recall_sources: {obs['recall_sources']}")
        print(
            f"  all_tool_calls: "
            f"{[(tc['name'], list(tc['args'].keys())) for tc in obs['all_tool_calls']]}"
        )
        print(f"  Verdict: {verdict}")
        results.append(obs)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    verdicts = [r["verdict"] for r in results]
    for v in (
        "verified",
        "refuted_class_b_a1",
        "refuted_class_b_a2",
        "inconclusive",
        "blocked",
    ):
        c = verdicts.count(v)
        print(f"  {v}: {c}/{N_RUNS}")

    out_path = Path("/tmp/s6_b20_results.json")
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
