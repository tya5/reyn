"""Materialise a real venv with the builtin ``rag`` plugin's own
``requirements.txt`` installed, and return its python interpreter's path.

#4302's option-A ruling: the rag plugin's two MCP servers (chunker /
vector-store, ``src/reyn/builtin/plugins/rag/scripts/``) import the
standalone ``fastmcp`` package, which is NOT a reyn dependency -- core
dropped it entirely in #4302, and register-only install (#3209) never
provisions a plugin's own deps either; the operator/LLM creates a
dedicated venv following the plugin's SETUP skill instructions, and
``mcp.json`` points the server's ``command`` at THAT venv's python.

Before this module, ``tests/builtin/test_fp0063_p3_rag_pipelines.py`` /
``test_fp0063_arc_witness.py`` spawned these servers via ``sys.executable``
-- the SAME venv running pytest -- which quietly relied on fastmcp being
installed there too. That collided with
``tests/mcp/test_fastmcp_core_dep_import_chain.py``'s own invariant
("fastmcp is NOT importable in a core install," #4302's actual goal) the
moment CI's install step was extended to cover it: one shared venv cannot
satisfy both "core has no fastmcp" and "the plugin's servers can import
fastmcp" at once. A dedicated venv resolves the contradiction AND is the
structurally correct shape -- it exercises the real per-plugin-venv
deployment model instead of reyn's own dev venv.

A plain, process-cached function (not a pytest fixture) so both the
``rag_plugin_python`` fixture in ``tests/conftest.py`` (for tests that
want it via normal fixture injection) and the fp0063 test files' own
``_write_project``-style helpers (plain functions, not fixtures, called
from many test bodies) share ONE build -- threading a fixture through
every one of those call sites would be pure churn for the same value.

Built once per pytest process (mirrors ``scripts/wheel_reachability_smoke.py``'s
``venv-rag-deps`` -- same real ``pip install -r requirements.txt``, same
network/disk cost already accepted there) and reused by every caller.
"""
from __future__ import annotations

import functools
import subprocess
import tempfile
import venv
from pathlib import Path


@functools.lru_cache(maxsize=1)
def rag_plugin_python() -> str:
    """Return the absolute path to a venv python with the rag plugin's
    ``requirements.txt`` installed, building it on first call."""
    import reyn

    repo_root = Path(reyn.__file__).resolve().parents[2]
    rag_requirements = (
        repo_root / "src" / "reyn" / "builtin" / "plugins" / "rag" / "requirements.txt"
    )
    venv_dir = Path(tempfile.mkdtemp(prefix="reyn-rag-plugin-venv-")) / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(str(venv_dir))
    bin_dir = venv_dir / "bin"
    if not bin_dir.exists():  # pragma: no cover - Windows layout
        bin_dir = venv_dir / "Scripts"
    python = bin_dir / "python"
    if not python.exists():  # pragma: no cover - Windows layout
        python = bin_dir / "python.exe"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "-r", str(rag_requirements)],
        check=True,
    )
    return str(python)
