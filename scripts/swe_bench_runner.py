"""swe_bench_runner.py — SWE-bench harness wrapper for Reyn.

Reads a single SWE-bench instance JSON, solves it with the general agent via
``reyn run-once`` in a per-instance container (the swe_bench skill was retired in
#187 — the agent iterates with its own tools, no test_patch), and emits the
harness-expected output shape on stdout. Authoritative scoring is delegated to the
external swebench harness (eval_benchmark.run_tier1_swebench_eval), downstream.

Usage
-----
    python scripts/swe_bench_runner.py --input instance.json [--model-name reyn] [--timeout 600]
    python scripts/swe_bench_runner.py --stdin

Input JSON fields (standard SWE-bench format)
---------------------------------------------
    instance_id      str  — e.g. "django__django-1234"
    repo             str  — e.g. "django/django"
    base_commit      str  — e.g. "abc123..."
    problem_statement str
    hints_text       str  — optional
    test_patch       str  — optional

Output (one JSON object on stdout)
------------------------------------
Success::

    {"instance_id": "...", "model_name_or_path": "reyn", "model_patch": "<git diff>"}

Failure (reyn non-zero, timeout, or unparseable output) — wrapper still
exits 0 so the harness batch keeps going::

    {"instance_id": "...", "model_name_or_path": "reyn", "error": "..."}

All progress / diagnostic messages go to stderr only.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

# Required keys every SWE-bench instance must carry.
_REQUIRED_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement")

# ── reyn-in-container venv provisioning (#183 / #1356 honored) ────────────────
#
# #1356 routes a python preprocessor step's harness subprocess through the
# sandbox backend — for `--env-backend=docker` that is a `docker exec` INTO the
# swebench instance image. The image's testbed conda python is repo-pinned
# (e.g. astropy = 3.9) and reyn needs >=3.11, so the harness cannot run there.
# Per the owner directive we provision a python3.11 venv WITH reyn inside the
# container (OpenHands-style: framework python separate from the repo env), and
# point the harness at it via REYN_HARNESS_PYTHON (the only OS-side change — a
# general 1-line env override in PythonRunner; this script is OS-change-free).
# pytest (sandboxed_exec) stays on the testbed conda — the agent's repo tests
# need the repo env, not the venv.
#
# Recipe (primary-evidence: real `docker run` on astropy-13453):
#   - base python3.11 already in the image (`/opt/miniconda3/bin/python3.11`);
#   - the container reaches PyPI (no wheelhouse needed) — `pip install` works;
#   - `pip install -e` on the :ro reyn mount FAILS (editable build can't write
#     to read-only source) → install reyn's deps explicitly + put reyn itself on
#     the path via a `.pth` to the bind-mounted source (no PYTHONPATH threading,
#     so container_backend.run is untouched). Deps are version-pinned by
#     reyn's own pyproject (read at runtime — no hardcoded drift).
# The reyn repo is bind-mounted at its OWN host path inside the container
# (`-v <repo>:<repo>:ro`) so host-absolute paths are valid in the container: the
# python step module path the OS hands the harness is the host skill_dir path
# (e.g. <repo>/src/reyn/stdlib/skills/swe_bench/escape_anchors.py), and the venv
# `.pth` points at <repo>/src — both resolve in-container only at the same path.
# (Mounting at a fixed /reyn would translate paths and break the host-absolute
# module path the harness loads.)
_CONTAINER_VENV = "/opt/reyn-venv"
_CONTAINER_PY311 = "/opt/miniconda3/bin/python3.11"
_CONTAINER_HARNESS_PYTHON = f"{_CONTAINER_VENV}/bin/python"
# reyn's pyproject lists test/lint tooling alongside runtime deps; the harness
# needs only the runtime set, so these dev tools are skipped (faster setup).
_DEV_ONLY_DEPS = frozenset(
    {"pytest", "pytest-cov", "pytest-xdist", "pytest-asyncio", "ruff", "mypy", "pre-commit"}
)


def _reyn_repo_root() -> Path:
    """The host reyn repo root (this script lives in <root>/scripts/)."""
    return Path(__file__).resolve().parent.parent


def reyn_runtime_deps(pyproject_text: str) -> list[str]:
    """Parse reyn's runtime dependencies from pyproject text (dev tools dropped).

    Pure (text in → list out) so it is unit-testable without the file or tomllib
    version specifics. Version pins are preserved verbatim from pyproject.
    """
    import tomllib

    data = tomllib.loads(pyproject_text)
    out = []
    for dep in data.get("project", {}).get("dependencies", []):
        # strip the version/extras to get the bare distribution name for the filter
        name = dep.split(">=")[0].split("==")[0].split("[")[0].split("<")[0].strip()
        if name.lower() not in _DEV_ONLY_DEPS:
            out.append(dep)
    return out


def provision_command(deps: list[str], reyn_src: str) -> str:
    """The `bash -lc` body that builds the in-container reyn venv (pure → str).

    Builds a python3.11 venv, installs reyn's runtime deps (version-pinned), and
    puts reyn itself on sys.path via a `.pth` to ``reyn_src`` (the bind-mounted
    source, at its host-absolute path inside the container) — so
    `<venv>/bin/python -m reyn.core.kernel._python_harness` imports reyn with no
    PYTHONPATH threading. Each dep is shell-quoted (version specs contain `>`)."""
    deps_arg = " ".join(shlex.quote(d) for d in deps)
    return (
        "set -e; "
        f"{_CONTAINER_PY311} -m venv {_CONTAINER_VENV}; "
        f"{_CONTAINER_VENV}/bin/pip install --quiet {deps_arg}; "
        f"echo {shlex.quote(reyn_src)} "
        f"> \"$({_CONTAINER_VENV}/bin/python -c 'import site; print(site.getsitepackages()[0])')/reyn.pth\""
    )


# ── pure helpers (testable without subprocess) ──────────────────────────────


def parse_input(text: str) -> dict[str, Any]:
    """Parse a JSON string into a SWE-bench instance dict.

    Raises
    ------
    ValueError
        If *text* is not valid JSON, or any required field is missing.
    """
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc

    if not isinstance(obj, dict):
        raise ValueError(f"expected a JSON object, got {type(obj).__name__}")

    missing = [f for f in _REQUIRED_FIELDS if not obj.get(f)]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")

    return obj


def format_output(
    instance_id: str,
    model_name: str,
    *,
    patch: str | None = None,
    error: str | None = None,
) -> str:
    """Serialise one harness output record as a JSON line.

    Exactly one of *patch* or *error* must be provided.
    """
    if patch is not None and error is None:
        obj: dict[str, str] = {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "model_patch": patch,
        }
    elif error is not None and patch is None:
        obj = {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "error": error,
        }
    else:
        raise ValueError("exactly one of patch or error must be supplied")

    return json.dumps(obj, ensure_ascii=False)


def _default_docker_runner(argv: list[str], *, timeout: int = 180):
    """Run a docker CLI command and return the CompletedProcess.

    Injectable in tests so the lifecycle is exercised without a real daemon.
    """
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def build_swe_task_prompt(instance: dict[str, Any]) -> str:
    """#187: a minimal, de-prescribed SWE task for the general agent.

    No procedure ("reproduce → verify → …") — the agent has tools (read / edit /
    grep / glob / sandboxed_exec) and decides how. NO test_patch (it is held out;
    the harness scores externally from the dataset). The agent fixes the issue in
    the working tree; the model_patch is the resulting ``git diff HEAD``.
    """
    repo = instance.get("repo", "")
    base = instance.get("base_commit", "")
    issue = (instance.get("problem_statement", "") or "").strip()
    hints = (instance.get("hints_text", "") or "").strip()
    lines = [
        f"This repository ({repo}, checked out at commit {base}) has the following "
        "open GitHub issue. Fix it in the working tree. You have file and shell "
        "tools; how you investigate and verify the fix is your judgment.",
        "",
        "## Issue",
        issue,
    ]
    if hints:
        lines += ["", "## Hints", hints]
    return "\n".join(lines)


def run_reyn_once_in_container(
    instance: dict[str, Any],
    *,
    image: str,
    repo_dir: str = "/testbed",
    docker_bin: str = "docker",
    timeout: int = 600,
    docker_runner=None,
    container_name: str | None = None,
    max_iterations: int = 80,
) -> dict[str, Any]:
    """#187: solve the SWE task with the GENERAL AGENT via ``reyn run-once``, NOT
    the swe_bench skill. The held-out test_patch is never given to the agent (it is
    not in the task prompt); the model_patch is the in-container ``git diff HEAD``
    after the agent finishes editing the working tree.

    Lifecycle mirrors :func:`run_reyn_in_container` (start container, provision the
    reyn venv, teardown always), but invokes ``reyn run-once --env-backend=docker
    --container <name> --grant-file-write --exclude-tools web__search,web__fetch``
    with the WHOLE SWE task piped to stdin as ONE message.

    ``reyn run-once`` reads the entire stdin as a single user turn (not the REPL's
    line-by-line read, which fragmented the 439-line task into 439 turns — the
    #1401 root cause) and drives the agent to completion via send_to_agent_impl
    (the same scoped chat session, no fresh unscoped build). --max-iterations is
    raised (default 80) so the autonomous agent can explore→edit→verify.
    """
    import uuid

    runner = docker_runner or _default_docker_runner
    instance_id = instance["instance_id"]
    name = container_name or f"reyn_swebench_once_{instance_id}_{uuid.uuid4().hex[:8]}"
    reyn_root = str(_reyn_repo_root())

    print(
        f"[swe_bench_runner] (once/#187) docker run image={image} name={name} "
        f"(instance={instance_id})",
        file=sys.stderr,
    )
    try:
        start = runner(
            [
                docker_bin, "run", "-d", "--name", name,
                "-v", f"{reyn_root}:{reyn_root}:ro",
                image, "sleep", "infinity",
            ],
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"docker run error: {exc}"}
    if start.returncode != 0:
        return {
            "ok": False,
            "error": f"docker run failed (rc={start.returncode}): {(start.stderr or '')[:400]}",
        }

    try:
        deps = reyn_runtime_deps((_reyn_repo_root() / "pyproject.toml").read_text())
        print(
            f"[swe_bench_runner] provisioning reyn venv in {name} ({len(deps)} deps)",
            file=sys.stderr,
        )
        prov = runner(
            [docker_bin, "exec", name, "bash", "-lc", provision_command(deps, f"{reyn_root}/src")],
            timeout=600,
        )
        if getattr(prov, "returncode", 1) != 0:
            return {
                "ok": False,
                "error": f"venv provisioning failed (rc={prov.returncode}): {(prov.stderr or '')[:400]}",
            }

        # Invoke the GENERAL AGENT via `reyn run-once`, piping the WHOLE SWE task
        # to stdin as ONE message (run-once reads the entire stdin as a single user
        # turn — not the REPL's line-by-line read that fragmented the task). The
        # scoped --grant-file-write lets the agent edit the in-container repo
        # working tree (sandbox ∩ bounds it to repo_dir); --exclude-tools hides web.
        # --state-dir keeps reyn's OS state host-side so it never lands on (and
        # pollutes) the in-container repo `git diff HEAD`. NO test_patch is in the
        # task (held out; the harness scores externally).
        task = build_swe_task_prompt(instance)
        state_dir = f"/tmp/reyn_once_state_{instance_id}_{name[-8:]}"
        once_cmd = [
            "reyn", "run-once",
            "--env-backend=docker",
            "--container", name,
            "--repo-dir", repo_dir,
            "--state-dir", state_dir,
            "--grant-file-write",
            # #187 faithful-eval: the agent solves from the issue + repo ONLY. Hide
            # web tools so it cannot web-search/fetch the gold PR/solution (the
            # benchmark answer) — matching SWE-agent/OpenHands (no web in-bench). The
            # exec network path is already sandbox-gated off; web is the only leak.
            "--exclude-tools", "web__search,web__fetch",
            # #1667 explicit opt-out: this is an external-repo task on /testbed, so
            # Reyn's own source (reyn_source__read/list/glob/grep self-help surface)
            # is irrelevant — hide the whole category at the catalog source so the
            # weak model doesn't misselect reyn_source__grep over file__* (it would
            # search Reyn's source, never the repo → empty patch). The interactive
            # agent keeps reyn_source (this is per-invocation explicit, owner 明示).
            "--exclude-categories", "reyn_source",
            # autonomous SWE needs many tool rounds (explore→edit→verify) > chat's 5.
            "--max-iterations", str(max_iterations),
        ]
        run_env = {**os.environ, "REYN_HARNESS_PYTHON": _CONTAINER_HARNESS_PYTHON}
        print(
            f"[swe_bench_runner] running: {' '.join(once_cmd)} (instance={instance_id})",
            file=sys.stderr,
        )
        try:
            subprocess.run(
                once_cmd,
                input=task,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=run_env,
            )
        except subprocess.TimeoutExpired:
            print(
                f"[swe_bench_runner] run-once timed out after {timeout}s — extracting diff",
                file=sys.stderr,
            )

        # Point-of-use discoverability: when a trace was captured this run, surface
        # the trace path + the analysis tool so it isn't overlooked (dogfood_trace.py
        # is THE trace tool — do not hand-parse the raw jsonl).
        _trace = run_env.get("REYN_LLM_TRACE_DUMP")
        if _trace:
            print(f"[swe_bench_runner] trace: {_trace}", file=sys.stderr)
            print(
                "[swe_bench_runner] analyze: python scripts/dogfood_trace.py "
                f"--mode llm-payloads --trace {_trace}",
                file=sys.stderr,
            )

        # model_patch = the agent's edits, as the in-container `git diff HEAD`.
        diff = runner(
            [docker_bin, "exec", name, "git", "-C", repo_dir, "diff", "HEAD"],
            timeout=120,
        )
        patch = getattr(diff, "stdout", "") if getattr(diff, "returncode", 1) == 0 else ""
        if isinstance(patch, bytes):
            patch = patch.decode("utf-8", "replace")
        return {"ok": True, "patch": patch}
    finally:
        try:
            rm = runner([docker_bin, "rm", "-f", name], timeout=120)
            if getattr(rm, "returncode", 0) != 0:
                print(
                    f"[swe_bench_runner] WARN teardown rm -f {name} rc={rm.returncode}: "
                    f"{(rm.stderr or '')[:200]}",
                    file=sys.stderr,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            print(
                f"[swe_bench_runner] WARN teardown rm -f {name} raised: {exc}",
                file=sys.stderr,
            )


# ── CLI entry point ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swe_bench_runner.py",
        description=(
            "Solve a SWE-bench instance with the general agent via `reyn run-once` "
            "for the SWE-bench evaluation harness (the swe_bench skill was retired "
            "in #187 — the agent iterates with its own tools, no test_patch). Reads "
            "a single SWE-bench instance and emits the harness-expected JSON on stdout."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--input", metavar="PATH",
        help="Path to a JSON file containing a single SWE-bench instance.",
    )
    source.add_argument(
        "--stdin", action="store_true",
        help="Read the SWE-bench instance JSON from stdin.",
    )

    p.add_argument(
        "--model-name", dest="model_name", default="reyn", metavar="NAME",
        help=(
            "Value for the harness 'model_name_or_path' field (default: reyn). "
            "Use a descriptive string so results are identifiable in harness output."
        ),
    )
    p.add_argument(
        "--timeout", type=int, default=600, metavar="SECONDS",
        help="Maximum seconds to wait for `reyn run` to complete (default: 600).",
    )
    # FP-0008 #1115 Stage 2 (β2b): faithful in-container run. When
    # --env-backend=docker, the runner owns the per-instance container lifecycle
    # (docker run the official SWE-bench image → reyn run inside it via the
    # generic --env-backend flags → teardown), so the general agent's
    # repo FS + commands execute against the pre-built /testbed, not the host.
    p.add_argument(
        "--env-backend", dest="env_backend", choices=["host", "docker"],
        default="host",
        help=(
            "Where the general agent's repo FS + commands run: 'host' (default) "
            "or 'docker' (per-instance container from --image; faithful in-container run)."
        ),
    )
    p.add_argument(
        "--image", dest="image", default=None, metavar="IMAGE",
        help=(
            "Docker image for --env-backend=docker — the pre-built SWE-bench "
            "instance image whose repo is checked out at --repo-dir (e.g. an "
            "official swebench/sweb.eval.* image). Required with --env-backend=docker."
        ),
    )
    p.add_argument(
        "--repo-dir", dest="repo_dir", default="/testbed", metavar="PATH",
        help="In-container repo working tree for --env-backend=docker (default: /testbed).",
    )
    p.add_argument(
        "--state-dir", dest="state_dir", default=None, metavar="PATH",
        help=(
            "Host-side OS state/artifacts dir for --env-backend=docker. "
            "Defaults to a per-run temp directory when omitted."
        ),
    )
    p.add_argument(
        "--docker-bin", dest="docker_bin", default="docker", metavar="BIN",
        help="Docker CLI binary for --env-backend=docker (default: docker).",
    )
    # #187: solve with the general agent (`reyn chat` / RouterLoop) instead of the
    # #187: the only solver is the general agent via `reyn run-once` (the swe_bench
    # SKILL was retired — it was the test_patch-leak cheat path). `--agent-mode` is
    # retained as 'chat'-only for back-compat with the dogfood harness invocation;
    # it requires --env-backend=docker (the agent's tools run in the instance
    # container). The authoritative scoring still uses the swebench HARNESS.
    p.add_argument(
        "--agent-mode", dest="agent_mode", choices=["chat"], default="chat",
        help=(
            "Solver mode (only 'chat' = the general agent via `reyn run-once`; the "
            "swe_bench skill cheat path was retired). Requires --env-backend=docker."
        ),
    )

    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns an integer exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # ── read input ────────────────────────────────────────────────────────────
    if args.stdin:
        raw = sys.stdin.read()
        source_label = "<stdin>"
    else:
        try:
            from pathlib import Path
            raw = Path(args.input).read_text(encoding="utf-8")
            source_label = args.input
        except OSError as exc:
            print(f"Error: cannot read input file: {exc}", file=sys.stderr)
            return 1

    # ── parse ─────────────────────────────────────────────────────────────────
    try:
        instance = parse_input(raw)
    except ValueError as exc:
        print(f"Error: invalid input ({source_label}): {exc}", file=sys.stderr)
        return 1

    instance_id = instance["instance_id"]

    # ── run the faithful general agent (`reyn run-once`) in a per-instance ──────
    # container. The swe_bench skill (and its host/skill subprocess paths) was
    # retired; the agent solves with its own tools (no test_patch) and the model
    # patch is the in-container `git diff HEAD`. Authoritative scoring is the
    # swebench HARNESS (separate, downstream).
    if getattr(args, "env_backend", "host") != "docker":
        print(
            "Error: faithful SWE eval requires --env-backend=docker "
            "(the agent's tools run in the per-instance container).",
            file=sys.stderr,
        )
        return 1
    if not args.image:
        print(
            "Error: --env-backend=docker requires --image "
            "(the pre-built SWE-bench instance image).",
            file=sys.stderr,
        )
        return 1
    result = run_reyn_once_in_container(
        instance,
        image=args.image,
        repo_dir=args.repo_dir,
        docker_bin=args.docker_bin,
        timeout=args.timeout,
    )

    # ── emit harness output ───────────────────────────────────────────────────
    if result["ok"]:
        line = format_output(instance_id, args.model_name, patch=result["patch"])
        print(line)
        print(
            f"[swe_bench_runner] done: {instance_id}",
            file=sys.stderr,
        )
    else:
        line = format_output(instance_id, args.model_name, error=result["error"])
        print(line)
        print(
            f"[swe_bench_runner] error: {instance_id}: {result['error']}",
            file=sys.stderr,
        )

    # Always exit 0 — the harness batch must continue on per-instance failures.
    return 0


if __name__ == "__main__":
    sys.exit(main())
