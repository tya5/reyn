"""Auto-register FakeEmbeddingProvider when REYN_EMBEDDING_PROVIDER=fake.

Loaded automatically by Python at startup if this directory is on PYTHONPATH.
The driver injects PYTHONPATH=<this dir>:<scripts> via env so any subprocess
spawned `reyn` inherits it.
"""
import os
import sys

if os.environ.get("REYN_EMBEDDING_PROVIDER") == "fake":
    try:
        # Make scripts/ importable
        _here = os.path.dirname(os.path.abspath(__file__))
        _scripts = os.path.dirname(_here)
        if _scripts not in sys.path:
            sys.path.insert(0, _scripts)
        from dogfood_rag_helper import register_fake_embedding_provider
        register_fake_embedding_provider()
    except Exception as exc:
        # Don't crash the interpreter — print to stderr
        sys.stderr.write(f"[sitecustomize_fake_embed] failed: {exc}\n")
