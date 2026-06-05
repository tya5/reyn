"""Tier 2: #183 root-fix — `runs_in: os` keeps OS-orchestration python steps host.

#1356 routes a python step's harness subprocess through a configured exec backend
(container / Seatbelt / Landlock). For an OS-orchestration step (pure
artifact-transform reyn framework code), that is over-binding: the agent's
sandbox/container (e.g. a swebench image's repo conda python) cannot host reyn.

The root-fix adds a general `PythonStep.runs_in` axis:
  - `sandbox` (default) — preserves #1356 (route to the backend when real).
  - `os` — never routed; runs in the host framework process.

Containment is unchanged on `os`: the safe-mode AST restriction + reyn.safe.file
path-gating apply identically on host (see the security-floor test).

No mocks of real collaborators: a real `_RecordingBackend` (SandboxBackend
interface) + real `PythonRunner` + real harness subprocess + real
`load_dsl_skill`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.python_runner import PythonRunner, PythonStepError
from reyn.sandbox.backend import SandboxResult


class _RecordingBackend:
    """Real (non-mock) SandboxBackend stand-in — records run() calls + returns a
    canned harness-success payload (routing observable without OS isolation)."""

    name = "seatbelt"  # a real backend (not "noop")

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None) -> SandboxResult:
        self.calls.append({"argv": list(argv)})
        payload = {"ok": True, "result": {"echoed": "ok"}}
        return SandboxResult(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")


def _write_module(skill_dir: Path, body: str) -> None:
    (skill_dir / "mod.py").write_text(body, encoding="utf-8")


# ── load-from-disk wiring (parser → model) ──────────────────────────────────


def test_loader_wires_runs_in_from_disk() -> None:
    """Tier 2: `runs_in` flows frontmatter→parser→model on a real disk load; the
    swe_bench text-prep python steps are `os`, an undeclared step defaults `sandbox`."""
    from reyn.compiler.loader import load_dsl_skill
    from reyn.schemas.models import PythonStep

    skill = load_dsl_skill(
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "swe_bench" / "skill.md"
    )
    py_steps = [
        s for phase in skill.phases.values()
        for s in phase.preprocessor if isinstance(s, PythonStep)
    ]
    assert py_steps, "swe_bench must have python preprocessor steps"
    assert all(s.runs_in == "os" for s in py_steps), (
        f"all swe_bench text-prep steps must declare runs_in:os; got "
        f"{[(s.module, s.runs_in) for s in py_steps]}"
    )
    # an undeclared step defaults to sandbox (#1356 preserved by default)
    default = PythonStep(
        type="python", module="./x.py", function="f", into="data.x",
        output_schema={"type": "object"},
    )
    assert default.runs_in == "sandbox"


# ── routing gate: os skips the backend / sandbox routes (falsification pair) ──


@pytest.mark.asyncio
async def test_runs_in_os_skips_backend(tmp_path: Path) -> None:
    """Tier 2: a `runs_in: os` step is NOT routed through the exec backend — it
    runs in the host process (real direct subprocess), returning its result; the
    backend is never called."""
    _write_module(tmp_path, "def go(artifact):\n    return {'host': True}\n")
    backend = _RecordingBackend()
    runner = PythonRunner()

    result = await runner.run(
        skill_dir=tmp_path, module="./mod.py", function="go", mode="safe",
        artifact={"data": {}}, timeout=30,
        sandbox_backend=backend, sandbox_policy={"network": False},
        runs_in="os",
    )

    assert backend.calls == [], "runs_in:os must NOT route to the exec backend"
    assert result == {"host": True}, "the host direct subprocess produced the result"


@pytest.mark.asyncio
async def test_default_sandbox_still_routes_to_backend(tmp_path: Path) -> None:
    """Tier 2: falsification — the default (`runs_in: sandbox`) step STILL routes
    through the exec backend with a real backend; #1356 is preserved, not reversed."""
    _write_module(tmp_path, "def go(artifact):\n    return {'echoed': 'ok'}\n")
    backend = _RecordingBackend()
    runner = PythonRunner()

    result = await runner.run(
        skill_dir=tmp_path, module="./mod.py", function="go", mode="safe",
        artifact={"data": {}}, timeout=30,
        sandbox_backend=backend, sandbox_policy={"network": False},
        # runs_in defaults to "sandbox"
    )

    assert backend.calls, "default step must route through backend.run (#1356)"
    assert "reyn.kernel._python_harness" in backend.calls[0]["argv"]
    assert result == {"echoed": "ok"}


# ── security floor: os does not weaken the safe.file gate ────────────────────


@pytest.mark.asyncio
async def test_runs_in_os_safe_file_still_path_gated(tmp_path: Path) -> None:
    """Tier 2: security floor — a `runs_in: os` step running on the host is still
    bound by reyn.safe.file's path gate; a write outside the forwarded
    file_write_paths is DENIED (host-run does not bypass containment)."""
    out_of_zone = tmp_path / "denied" / "evil.txt"
    _write_module(
        tmp_path,
        "from reyn.safe import file as _f\n"
        f"def go(artifact):\n"
        f"    _f.write({str(out_of_zone)!r}, 'x')\n"
        f"    return {{'wrote': True}}\n",
    )
    runner = PythonRunner()

    with pytest.raises(PythonStepError):
        await runner.run(
            skill_dir=tmp_path, module="./mod.py", function="go", mode="safe",
            artifact={"data": {}}, timeout=30,
            allowed_modules=["reyn.safe.file"],
            # grant a DIFFERENT dir only; the write target is out of zone
            file_write_paths=[str(tmp_path / "allowed")],
            runs_in="os",
        )
    assert not out_of_zone.exists(), "the out-of-zone write must not have landed"
