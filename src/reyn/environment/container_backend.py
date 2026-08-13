"""DockerEnvironmentBackend — repo FS + exec INSIDE a Docker container (FP-0008 #1115 Stage 2).

One class implementing BOTH Protocols (案C-pure):
  - :class:`~reyn.environment.backend.EnvironmentBackend` — repo filesystem ops
    run *inside* the container against ``repo_dir`` (e.g. ``/testbed``);
  - :class:`~reyn.security.sandbox.backend.SandboxBackend` — ``run()`` exec inside the
    same container.

Injecting the SAME instance at ``Workspace.environment_backend`` (FS) and
``OpContext.sandbox_backend`` (exec) makes file edits + commands hit one
container target — the agent edits ``/testbed`` directly, so there is **no
host-diff bridge** (unlike the interim FP-0017/PR-A apply-into-prebuilt
approach, whose ``git diff host → reset → apply into container`` logic is
deliberately DROPPED here — that bridge, and the per-call reset / tracked-
untracked gymnastics, were artifacts of file-on-host / exec-in-container
divergence).
``run()`` is a plain ``docker exec`` because the files are already in
``repo_dir``.

Fidelity: FS ops are executed as ``docker exec <c> python3 -c <script> <args>``
so the container reproduces the EXACT Python filesystem semantics of
:class:`~reyn.environment.host_backend.HostBackend` (stat dict shape, ``glob``
recursive ``**``, ``grep`` Python-``re`` matching) rather than shell tools whose
semantics differ. This is the exec-per-op MVP (one ``docker exec`` per FS op);
a persistent in-container IO-responder is a later optimization.

Axis-agnostic / P7-clean: no domain-specific strings; bound to a
``(container, repo_dir)`` pair. FS uses a sync runner (the EnvironmentBackend
Protocol is sync, matching Workspace); ``run()`` uses an async runner (the
SandboxBackend Protocol is async). Both are injectable so the orchestration is
unit-testable without a live Docker daemon.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Pattern

from reyn.environment.backend import GrepResult
from reyn.security.sandbox._subprocess_io import MAX_SUBPROCESS_OUTPUT_BYTES, communicate_capped
from reyn.security.sandbox.backend import (
    AxisEnforcement,
    AxisEnforcementDeclaration,
    SandboxResult,
    WrappedCommand,
)
from reyn.security.sandbox.policy import POST_KILL_DRAIN_GRACE_SECONDS, SandboxPolicy

# Sync runner: execute argv (optionally stdin), return SandboxResult. Injected so
# the FS-op orchestration is testable without Docker; default = _sync_runner.
SyncRunner = Callable[..., SandboxResult]
# Async runner: same contract for run() (mirrors the PR-A backend runner).
AsyncRunner = Callable[..., Awaitable[SandboxResult]]


def _sync_runner(
    argv: list[str],
    *,
    stdin: bytes | None = None,
    timeout: int | None = None,
    max_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES,
) -> SandboxResult:
    """Real sync runner: spawn argv via ``subprocess.Popen`` + the SAME capped
    reader every other sandbox backend uses (``communicate_capped``, #3822/
    #3837's own finding: Docker was the one launch route still doing a plain
    ``capture_output=True`` — unbounded — read, the exact shape #3837 fixed
    for CodeAct). A flooding in-container process can no longer OOM the host."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        return SandboxResult(returncode=-1, stdout=b"", stderr=str(exc).encode())
    try:
        stdout_b, stderr_b, truncated = communicate_capped(
            proc, input=stdin, max_bytes=max_bytes, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        stdout_b = exc.stdout if isinstance(exc.stdout, bytes) else b""
        stderr_b = exc.stderr if isinstance(exc.stderr, bytes) else b""
        return SandboxResult(
            returncode=-1, stdout=stdout_b,
            stderr=stderr_b + f"\ntimed out after {timeout}s".encode(),
        )
    return SandboxResult(
        returncode=proc.returncode, stdout=stdout_b, stderr=stderr_b, truncated=truncated,
    )


async def _async_runner(
    argv: list[str],
    *,
    stdin: bytes | None = None,
    timeout: int | None = None,
    max_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES,
) -> SandboxResult:
    """Real async runner for run() — mirrors ``_sync_runner`` above (and every
    other sandbox backend's own blocking-path pattern: a real ``Popen`` drained
    off the event loop via ``run_in_executor``, not ``asyncio.create_subprocess_exec``
    + plain ``communicate()``, so the SAME output cap applies here (#3822/#3837).
    ``cancel_event`` support is a separate, larger follow-up (#3822's own
    measurement recorded this as a distinct gap) — this fix is output-cap only."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        return SandboxResult(returncode=-1, stdout=b"", stderr=str(exc).encode())

    loop = asyncio.get_running_loop()

    def _drain() -> SandboxResult:
        try:
            stdout_b, stderr_b, truncated = communicate_capped(
                proc, input=stdin, max_bytes=max_bytes, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            stdout_b = exc.stdout if isinstance(exc.stdout, bytes) else b""
            stderr_b = exc.stderr if isinstance(exc.stderr, bytes) else b""
            return SandboxResult(
                returncode=-1, stdout=stdout_b,
                stderr=stderr_b + f"\ntimed out after {timeout}s".encode(),
            )
        return SandboxResult(
            returncode=proc.returncode, stdout=stdout_b, stderr=stderr_b, truncated=truncated,
        )

    return await loop.run_in_executor(None, _drain)


# KillInContainer: read the in-container PID (left in *pidfile* by run()'s
# cancel-aware exec wrapper), signal it, verify it is actually gone. Injected
# so cancel behavior is unit-testable without a live Docker daemon; default =
# _docker_kill_in_container.
KillInContainer = Callable[..., Awaitable[bool]]


async def _docker_kill_in_container(
    docker_bin: str, container: str, pidfile: str, *, grace_seconds: float = 2.0,
) -> bool:
    """#3862: killing the HOST-side ``docker exec`` client process does NOT
    reliably kill the process running INSIDE the container — the two are
    only connected by an I/O stream, not a process/signal relationship, so a
    client-side ``kill_process_tree`` alone is a "sent, not received" fix
    (the exact trap lead-coder flagged). This signals the REAL in-container
    PID via a SEPARATE ``docker exec ... kill`` call, SIGTERM first, then
    SIGKILL after ``grace_seconds`` if still alive — mirrors
    ``kill_process_tree``'s own grace-then-force shape — and returns whether
    a final liveness check confirms the process is actually gone (the
    "stopped, not just signalled" witness).

    Best-effort throughout: cancellation must not raise past this point — a
    read/signal/liveness-check failure (container already gone, pidfile
    missing because the workload exited before writing it, exec itself
    denied) is swallowed and reported as ``False`` (could not verify), never
    propagated.
    """
    loop = asyncio.get_running_loop()

    def _docker_exec(*args: str) -> "subprocess.CompletedProcess[bytes]":
        return subprocess.run(
            [docker_bin, "exec", container, *args], capture_output=True, timeout=5,
        )

    try:
        cat = await loop.run_in_executor(None, lambda: _docker_exec("cat", pidfile))
        if cat.returncode != 0:
            return False
        cpid = cat.stdout.decode().strip()
        if not cpid.isdigit():
            return False
    except Exception:  # noqa: BLE001 — cancellation must not raise
        return False

    async def _alive() -> bool:
        try:
            probe = await loop.run_in_executor(None, lambda: _docker_exec("kill", "-0", cpid))
        except Exception:  # noqa: BLE001
            return False
        return probe.returncode == 0

    try:
        await loop.run_in_executor(None, lambda: _docker_exec("kill", "-TERM", cpid))
    except Exception:  # noqa: BLE001
        pass

    deadline = loop.time() + grace_seconds
    while loop.time() < deadline:
        if not await _alive():
            return True
        await asyncio.sleep(0.1)

    try:
        await loop.run_in_executor(None, lambda: _docker_exec("kill", "-KILL", cpid))
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(0.2)
    return not await _alive()


# ── In-container Python snippets (read args from sys.argv; emit on stdout) ────
# Paths are passed as argv (NOT interpolated into the script) so no shell/Python
# quoting hazard. Structured results are JSON on stdout; bytes are raw stdout.

_READ = (
    "import sys,os\n"
    "p=sys.argv[1]\n"
    "if not os.path.exists(p): sys.exit(7)\n"
    "sys.stdout.buffer.write(open(p,'rb').read())\n"
)
_WRITE = (
    "import sys,os\n"
    "p=sys.argv[1]\n"
    "d=os.path.dirname(p)\n"
    "if d: os.makedirs(d,exist_ok=True)\n"
    "open(p,'wb').write(sys.stdin.buffer.read())\n"
)
_DELETE = (
    "import sys,os\n"
    "p=sys.argv[1]\n"
    "if os.path.exists(p) and os.path.isfile(p):\n"
    "  os.unlink(p); print('1')\n"
    "else:\n"
    "  print('0')\n"
)
_MKDIR = (
    "import sys,os\n"
    "p=sys.argv[1]; parents=sys.argv[2]=='1'\n"
    "if os.path.exists(p):\n"
    "  print('exists' if os.path.isdir(p) else 'notdir')\n"
    "else:\n"
    "  (os.makedirs if parents else os.mkdir)(p)\n"
    "  print('created')\n"
)
_MOVE = (
    "import sys,os,shutil\n"
    "s=sys.argv[1]; d=sys.argv[2]\n"
    "if not os.path.exists(s):\n"
    "  print('0')\n"
    "else:\n"
    "  pd=os.path.dirname(d)\n"
    "  os.makedirs(pd,exist_ok=True) if pd else None\n"
    "  shutil.move(s,d); print('1')\n"
)
_STAT = (
    "import sys,os,json\n"
    "p=sys.argv[1]\n"
    "if not os.path.exists(p):\n"
    "  print('null')\n"
    "else:\n"
    "  st=os.stat(p)\n"
    "  print(json.dumps({'size':st.st_size,'mtime':st.st_mtime,'ctime':st.st_ctime,"
    "'is_dir':os.path.isdir(p),'is_file':os.path.isfile(p),'mode':oct(st.st_mode & 0o777)}))\n"
)
# Returns matching FILES only (directories excluded), filtered in-container —
# symmetric with _GREP's `f.is_file()` below. The Workspace consumer
# (glob_files) wants files only, and a host-side filter cannot stat container
# paths (#1375 D10), so the file-filter must run in the same environment as the
# match. See backend.glob's Protocol docstring for the contract rationale.
_GLOB = (
    "import sys,glob,json,os,pathlib\n"
    "pat=sys.argv[1]; root=sys.argv[2]\n"
    "if root:\n"
    "  res=[str(x) for x in pathlib.Path(root).glob(pat) if x.is_file()]\n"
    "else:\n"
    "  res=[p for p in glob.glob(pat,recursive=True) if os.path.isfile(p)]\n"
    "print(json.dumps(res))\n"
)
# grep: argv = pattern, flags, root, glob_or_'', file_type_or_'', output_mode,
#       head_limit_or_'-1', context_before, context_after
#
# #1452 encoding note (deliberate scope boundary): this grep runs as a
# stdlib-only python script in the TARGET container, where REYN's
# charset-normalizer dependency is not guaranteed to exist. So it keeps
# ``read_text('utf-8','replace')`` — legacy-encoding detection (SJIS / EUC-JP /
# UTF-16) and the binary-skip ladder are HOST-only (host_backend.py via
# workspace/text_codec). In-container grep therefore matches UTF-8 content
# faithfully but may replacement-char a non-UTF-8 file's bytes. Acceptable: the
# faithful-SWE container path is for source repos (overwhelmingly UTF-8), and
# adding charset-normalizer to arbitrary target images is out of scope.
_GREP = (
    "import sys,re,json,os,pathlib\n"
    "pat,flags,root,g,ft,mode,hl,cb,ca=sys.argv[1:10]\n"
    "rx=re.compile(pat,int(flags)); hl=int(hl); cb=int(cb); ca=int(ca)\n"
    "rp=pathlib.Path(root)\n"
    "cands=[rp] if rp.is_file() else sorted(f for f in rp.glob(g or '**/*') if f.is_file())\n"
    "cands=[f for f in cands if (not ft or f.suffix.lstrip('.')==ft.lstrip('.'))]\n"
    "out={'output_mode':mode,'files':[],'count':0,'matches':[]}\n"
    "if mode=='files_with_matches':\n"
    "  for f in cands:\n"
    "    try:\n"
    "      if rx.search(f.read_text('utf-8','replace')): out['files'].append(str(f))\n"
    "    except OSError: pass\n"
    "elif mode=='count':\n"
    "  t=0\n"
    "  for f in cands:\n"
    "    try: t+=len(rx.findall(f.read_text('utf-8','replace')))\n"
    "    except OSError: pass\n"
    "  out['count']=t\n"
    "else:\n"
    "  done=False\n"
    "  for f in cands:\n"
    "    if done: break\n"
    "    try: lines=f.read_text('utf-8','replace').splitlines()\n"
    "    except OSError: continue\n"
    "    for i,line in enumerate(lines):\n"
    "      if not rx.search(line): continue\n"
    "      e={'path':str(f),'line_number':i+1,'content':line}\n"
    "      if cb or ca:\n"
    "        s=max(0,i-cb); en=min(len(lines),i+ca+1)\n"
    "        e['context']=[{'line_number':j+1,'content':lines[j],'is_match':j==i} for j in range(s,en)]\n"
    "      out['matches'].append(e)\n"
    "      if hl>=0 and len(out['matches'])>=hl: done=True; break\n"
    "print(json.dumps(out))\n"
)

# #1481: probe the CONTAINER environment for the SP Environment section. Each
# field is independently guarded so a single failure omits only that key (the
# degrade contract) — OS family / kernel / shell / .git all come from inside the
# container, never the host.
_ENV_INFO = (
    "import sys,os,json,platform\n"
    "repo=sys.argv[1] if len(sys.argv)>1 else os.getcwd()\n"
    "out={}\n"
    "try: out['platform']=platform.system().lower()\n"
    "except Exception: pass\n"
    "try: out['os_version']=platform.release()\n"
    "except Exception: pass\n"
    "sh=os.environ.get('SHELL','')\n"
    "if sh: out['shell']=sh\n"
    # os.path.exists (not isdir) for host parity: a git worktree's .git is a
    # FILE (a gitdir pointer), which isdir would mis-judge as not-a-repo.
    "try: out['is_git_repo']=os.path.exists(os.path.join(repo,'.git'))\n"
    "except Exception: pass\n"
    "print(json.dumps(out))\n"
)


class DockerEnvironmentBackend:
    """Repo FS + exec inside a Docker container (dual-Protocol, bridge-free)."""

    name: str = "docker"

    # #4039 (D1/D2 — architect's "sharpest instance" example): every axis is
    # DOES_NOT_ENFORCE. run() reads only policy.timeout_seconds /
    # policy.max_output_bytes (its own docstring); write/network isolation
    # comes from FIXED container-launch flags (--read-only, --tmpfs /tmp,
    # --network none), not from the policy fields an operator writes —
    # measured directly (#4042, real execution): deny_subprocess=True does
    # not stop a nested spawn, env_deny_names has nothing to filter (the
    # container never sees host env at all — #4042/#4047). Docker is the
    # backend that most needs this declaration: unlike Noop (whose name and
    # docstring warn a reader to expect no enforcement), an operator choosing
    # Docker specifically FOR isolation has no reason to suspect these axes
    # pass straight through.
    enforced_axes: AxisEnforcementDeclaration = AxisEnforcementDeclaration(
        write_paths=AxisEnforcement.DOES_NOT_ENFORCE,
        write_deny_paths=AxisEnforcement.DOES_NOT_ENFORCE,
        read_deny_paths=AxisEnforcement.DOES_NOT_ENFORCE,
        network=AxisEnforcement.DOES_NOT_ENFORCE,
        deny_subprocess=AxisEnforcement.DOES_NOT_ENFORCE,
        env_deny_names=AxisEnforcement.DOES_NOT_ENFORCE,
        allow_env_names=AxisEnforcement.DOES_NOT_ENFORCE,
    )

    def __init__(
        self,
        *,
        container: str,
        repo_dir: str,
        docker_bin: str = "docker",
        python_bin: str = "python3",
        fs_runner: SyncRunner | None = None,
        runner: AsyncRunner | None = None,
        kill_in_container: KillInContainer | None = None,
    ) -> None:
        self.container = container
        self.repo_dir = repo_dir
        self.docker_bin = docker_bin
        self.python_bin = python_bin
        self._fs_runner: SyncRunner = fs_runner or _sync_runner
        self._runner: AsyncRunner = runner or _async_runner
        # #3862: injected so cancel behavior is unit-testable without a live
        # Docker daemon. See _docker_kill_in_container's own docstring for
        # why this is a separate step from killing the host-side client.
        self._kill_in_container: KillInContainer = kill_in_container or _docker_kill_in_container

    # ── helpers ───────────────────────────────────────────────────────────────

    def _py(self, script: str, *args: str, stdin: bytes | None = None) -> SandboxResult:
        # `python3 -c CODE a b` → sys.argv == ['-c', 'a', 'b'] (args start at [1]).
        # Paths/patterns go as argv (NOT interpolated into CODE) — quote/newline
        # safe + no injection (lead-coder Stage 2 review-gate).
        argv = [
            self.docker_bin, "exec", *(["-i"] if stdin is not None else []),
            self.container, self.python_bin, "-c", script, *args,
        ]
        return self._fs_runner(argv, stdin=stdin)

    @staticmethod
    def _ok(res: SandboxResult) -> bool:
        return res.returncode == 0

    # ── EnvironmentBackend (FS, sync — executed in-container) ──────────────────

    def read_bytes(self, path: Path) -> bytes | None:
        res = self._py(_READ, str(path))
        if res.returncode == 7:
            return None
        if res.returncode != 0:
            return None
        return res.stdout

    def write_bytes(self, path: Path, data: bytes) -> None:
        res = self._py(_WRITE, str(path), stdin=data)
        if not self._ok(res):
            raise OSError(f"container write failed for {path}: {res.stderr.decode('utf-8','replace')}")

    def delete(self, path: Path) -> bool:
        res = self._py(_DELETE, str(path))
        return self._ok(res) and res.stdout.strip() == b"1"

    def mkdir(self, path: Path, *, parents: bool = True) -> bool:
        res = self._py(_MKDIR, str(path), "1" if parents else "0")
        token = res.stdout.strip()
        if token == b"notdir":
            raise FileExistsError(f"path exists but is not a directory: {str(path)!r}")
        return token == b"created"

    def move(self, src: Path, dst: Path) -> bool:
        res = self._py(_MOVE, str(src), str(dst))
        return self._ok(res) and res.stdout.strip() == b"1"

    def stat(self, path: Path) -> dict | None:
        res = self._py(_STAT, str(path))
        if not self._ok(res):
            return None
        payload = res.stdout.decode("utf-8", "replace").strip()
        if payload == "null" or not payload:
            return None
        return json.loads(payload)

    def glob(self, pattern: str, *, root: Path | None = None) -> list[Path]:
        res = self._py(_GLOB, pattern, str(root) if root is not None else "")
        if not self._ok(res):
            return []
        return [Path(s) for s in json.loads(res.stdout.decode("utf-8", "replace") or "[]")]

    def grep(
        self,
        root: Path,
        regex: Pattern[str],
        *,
        glob: str | None = None,
        file_type: str | None = None,
        output_mode: str = "content",
        head_limit: int | None = None,
        context_before: int = 0,
        context_after: int = 0,
    ) -> GrepResult:
        res = self._py(
            _GREP,
            regex.pattern, str(regex.flags), str(root), glob or "", file_type or "",
            output_mode, str(head_limit if head_limit is not None else -1),
            str(context_before), str(context_after),
        )
        if not self._ok(res):
            return GrepResult(output_mode=output_mode)
        data: dict[str, Any] = json.loads(res.stdout.decode("utf-8", "replace") or "{}")
        return GrepResult(
            output_mode=data.get("output_mode", output_mode),
            files=[Path(s) for s in data.get("files", [])],
            count=int(data.get("count", 0)),
            matches=[{**m, "path": Path(m["path"])} for m in data.get("matches", [])],
        )

    # ── Environment info (#1481 — in-container probe for SP Environment) ───────

    def get_environment_info(self) -> dict:
        """Probe the CONTAINER environment for the SP Environment section (#1481).

        Runs a single ``python3 -c`` in-container (via the sync FS runner) to
        collect ``platform`` / ``os_version`` / ``shell`` / ``is_git_repo`` from
        the container OS — NOT the host. The host adapter's non-host branch
        (router_host_adapter.get_environment_info) consumes these.

        Degrade contract (#1477 host-value-leak prevention): a probe that fails
        is OMITTED from the result — never back-filled with a host value (e.g.
        showing host ``darwin`` / ``zsh`` for a Linux container). A full exec
        failure (no container / docker error) returns ``{}`` so the adapter
        omits every host-derived field rather than guessing.
        """
        res = self._py(_ENV_INFO, self.repo_dir)
        if not self._ok(res):
            return {}
        try:
            info = json.loads(res.stdout.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return info if isinstance(info, dict) else {}

    # ── SandboxBackend (exec, async — plain container exec, NO bridge) ─────────

    def available(self) -> bool:
        """True when the docker binary exists and the daemon is reachable."""
        if shutil.which(self.docker_bin) is None:
            return False
        try:
            completed = subprocess.run(
                [self.docker_bin, "info"], capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def self_test(self) -> str | None:
        """Always None — this backend is OUTSIDE the #2983 enforcement self-test,
        and says so rather than pretending to have been witnessed.

        The probes attempt a HOST filesystem write outside ``write_paths``, and a
        HOST process spawn under ``deny_subprocess=True``, and require a refusal
        of each. Neither question translates here: the container itself is the
        isolation boundary, and this backend scopes policy to the fidelity
        boundary rather than enforcing the host-path / host-syscall model the
        probes assume (see ``SandboxBackend.run``'s note on workspace-coupled
        backends). Running them as-is would ask the wrong question and fail a
        container that is isolating perfectly well.

        Returning None is safe here only because nothing consults it: this backend
        is INJECTED (``session.py``'s ``sandbox_backend`` / ``environment_backend``
        seam), never resolved through ``get_default_backend()``, so it never
        reaches the ``_verify`` gate that applies ``on_unsupported``. It is
        implemented at all because the Protocol requires every backend to answer —
        a backend that stays silent would otherwise be the next thing to claim an
        enforcement nobody checked.

        So: container isolation remains UNWITNESSED by any self-test, exactly as
        it was before #2983. That is a known stage-1 gap, not a claim of health.
        """
        return None

    def probe_binary(self) -> "list[str] | None":
        """#4364 PR-2: always ``None`` — architect's own worked example for
        why this method exists at all (#4364 issue thread): the image an
        operator configured is not something this backend can assume ships
        ``true`` (or any other specific binary) at a known path, and
        probing would mean a HOST-side ``shutil.which`` lookup that has no
        bearing on what the CONTAINER's own filesystem actually contains.
        Same "measure, don't assert" reasoning ``self_test`` above already
        states for this backend: a guess here would be exactly the kind of
        claim-without-witness this whole feature exists to avoid."""
        return None

    def session_artifact_outside_write_scope(self, policy: SandboxPolicy) -> bool:
        """Vacuously True (#4434): neither ``wrap_command`` nor ``run`` below
        writes a policy-derived representation to disk — the policy travels
        into fixed container-launch flags (``--read-only``/``--tmpfs
        /tmp``/``--network none``) baked at container-creation time, not
        into a file this backend re-reads per call — so there is no on-disk
        artifact a sandboxed child could rewrite. Still bears the contract
        (owner ruling, #4434: the sandbox abstraction means every backend
        answers it, not just the ones that currently have something to
        cache) — caught missing this method entirely on #4439's first CI
        run (this class lives in ``environment/``, a different directory
        from the other 3 backends, and a hand-typed backend census missed
        it; see ``test_sandbox_session_artifact_contract_4434.py``'s
        registry-derived census for the structural fix)."""
        return True

    def wrap_command(self, argv: list[str], policy: SandboxPolicy) -> WrappedCommand:
        """Prepend a ``docker exec`` invocation to *argv* for a PERSISTENT-process
        launch (e.g. a stdio MCP server, #2620) inside the SAME container
        ``run()`` execs into. Mirrors ``run()``'s login-shell + argv-faithful
        re-exec construction (see that method's docstring for the ``bash -lc
        'exec "$@"'`` rationale); ``-i`` is always passed (unlike ``run()``,
        which only opens stdin when the caller supplies some) because a
        persistent stdio server holds bidirectional pipes open for its whole
        lifetime. No cleanup resource is owned (unlike Seatbelt's temp profile).

        ``env``, unlike every other backend's ``wrap_command()`` (#3822): the
        returned ``argv`` is a HOST-side ``docker`` CLI invocation, not the
        sandboxed workload itself — the workload's actual env comes from the
        container IMAGE's own login-shell activation (conda/nvm/pyenv), a
        deliberate fidelity boundary this class's ``run()`` already documents
        ("Honors only policy.timeout_seconds"). ``policy.env_deny_names``
        (renamed #3901 PR-B ④) has no meaning for a host-side ``docker exec``
        invocation, so fabricating a filtered env here would be inventing a
        value with
        nothing to scope. The honest answer for what the HOST ``docker`` CLI
        itself needs (``DOCKER_HOST`` / ``HOME`` / ``PATH`` / docker config
        discovery) is "the same as `run()`'s own runners already assume" —
        neither ``_sync_runner`` nor ``_async_runner`` passes ``env=`` at
        all, i.e. full host inherit for the CLI call itself. Returning that
        SAME choice here keeps ``wrap_command()`` and ``run()`` consistent
        with each other rather than inventing a THIRD, novel policy."""
        wrapped_argv = [
            self.docker_bin, "exec", "-i",
            "-w", self.repo_dir, self.container,
            "bash", "-lc", 'exec "$@"', "reyn-exec", *argv,
        ]
        return WrappedCommand(argv=wrapped_argv, env=dict(os.environ), cleanup=None)

    async def run(
        self, argv: list[str], policy: SandboxPolicy, *, stdin: bytes | None = None,
        cwd: str | None = None, cancel_event: "asyncio.Event | None" = None,
    ) -> SandboxResult:
        """``docker exec`` of argv (via a login shell) with cwd=repo_dir — NO host-diff bridge.

        The files are already in ``repo_dir`` (the agent edited them via the FS
        methods above), so there is nothing to sync in. Honors
        ``policy.timeout_seconds`` and (#3822) ``policy.max_output_bytes`` —
        the fidelity boundary (as in PR-A) applies only to env/cwd, not to
        the output-cap/timeout/cancel every other launch route already
        shares.

        The host-side ``cwd`` (= the OS's ``workspace.base_dir``) is **ignored**:
        the repo lives at the in-container ``self.repo_dir`` (``-w``), which a
        host path can't address. Same asymmetry as policy enforcement — a
        workspace-coupled backend scopes both to the fidelity boundary.
        """
        # Run inside a LOGIN shell so the image's env-activation (conda / nvm /
        # rbenv / pyenv — set up in /etc/profile or ~/.bash_profile/~/.bashrc)
        # is in effect. A plain ``docker exec <argv>`` uses only the base PATH
        # and misses login-activated tooling — e.g. a SWE-bench image installs
        # pytest into a ``conda activate``-d env, so ``python -m pytest`` under a
        # direct exec resolves the base python (no pytest) and fails. This is a
        # generic correctness fix: the backend knows nothing image-specific, it
        # just honors whatever the image's login profile activates.
        #
        # ``bash -lc 'exec "$@"' reyn-exec <argv>`` passes argv as positional
        # params ($1..), NOT spliced into the script text, so there is no
        # shell-injection / quoting surface (``"$@"`` re-exec is argv-faithful).
        # `-i` keeps stdin open through `docker exec` so a process that reads
        # stdin (the python-step harness reads its JSON request there) receives
        # it — without `-i`, docker exec drops the host-piped stdin and the
        # in-container process sees EOF ("harness received empty stdin"). Mirrors
        # the `_py` FS-helper above; only when stdin is provided (sandboxed_exec
        # passes none → unchanged).
        if cancel_event is None:
            # No cancel support requested: original path, byte-identical.
            exec_argv = [
                self.docker_bin, "exec", *(["-i"] if stdin is not None else []),
                "-w", self.repo_dir, self.container,
                "bash", "-lc", 'exec "$@"', "reyn-exec", *argv,
            ]
            return await self._runner(
                exec_argv, stdin=stdin, timeout=policy.timeout_seconds,
                max_bytes=policy.max_output_bytes,
            )

        # #3862 cancel-aware path. Killing the HOST-side `docker exec` client
        # does NOT reliably kill the process INSIDE the container (see
        # _docker_kill_in_container's docstring) — so the wrapper script also
        # records the in-container PID to a per-invocation pidfile BEFORE
        # exec'ing into the real command (`echo $$` before `exec` reports the
        # PID the exec'd process keeps, since exec replaces the image, not
        # the PID). `"$1"` is the pidfile, `shift` drops it so `"$@"` is
        # still argv-faithful for the real command — no new shell-injection
        # surface versus the no-cancel path above.
        pidfile = f"/tmp/.reyn-exec-{uuid.uuid4().hex}.pid"
        exec_argv = [
            self.docker_bin, "exec", *(["-i"] if stdin is not None else []),
            "-w", self.repo_dir, self.container,
            "bash", "-lc", 'echo $$ > "$1"; shift; exec "$@"',
            "reyn-exec", pidfile, *argv,
        ]
        try:
            proc = subprocess.Popen(
                exec_argv,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            return SandboxResult(returncode=-1, stdout=b"", stderr=str(exc).encode())

        if stdin is not None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except OSError:
                pass

        loop = asyncio.get_running_loop()
        # #4271/#4277: the inner communicate_capped timeout must be STRICTLY
        # LARGER than the outer asyncio.wait's own timeout below — same value
        # means whichever deadline is reached first decides the outcome, not
        # the outer one that OWNS it. A same-value regression let
        # subprocess.TimeoutExpired escape uncaught through the plain
        # `await comm_future` in the normal-completion branch below (#4277 CI
        # RED, same shape in codeact_runner.py). The inner value only needs
        # to guarantee "never unbounded" — the outer wait already enforces
        # the real deadline.
        comm_future: asyncio.Future = loop.run_in_executor(
            None,
            lambda: communicate_capped(
                proc, max_bytes=policy.max_output_bytes,
                timeout=policy.timeout_seconds + POST_KILL_DRAIN_GRACE_SECONDS,
            ),
        )
        cancel_task = asyncio.create_task(cancel_event.wait())

        done, _ = await asyncio.wait(
            {comm_future, cancel_task},
            timeout=policy.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if cancel_task in done:
            # #3862: signal the REAL in-container process, not just the host
            # client — "stopped", not "signal sent", is the witness.
            verified_stopped = await self._kill_in_container(
                self.docker_bin, self.container, pidfile,
            )
            proc.kill()  # host-side client cleanup; does not itself stop the workload
            try:
                stdout_b, stderr_b, _trunc = await asyncio.wait_for(
                    asyncio.shield(comm_future), timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception):
                stdout_b, stderr_b, _trunc = b"", b"", False
            return SandboxResult(
                returncode=-int(signal.SIGTERM),
                stdout=stdout_b or b"", stderr=stderr_b or b"",
                truncated=_trunc, cancelled=verified_stopped,
            )
        elif not done:
            cancel_task.cancel()
            await self._kill_in_container(self.docker_bin, self.container, pidfile)
            proc.kill()
            try:
                stdout_b, stderr_b, _trunc = await asyncio.wait_for(
                    asyncio.shield(comm_future), timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception):
                stdout_b, stderr_b, _trunc = b"", b"", False
            return SandboxResult(
                returncode=-1, stdout=stdout_b or b"",
                stderr=(stderr_b or b"") + f"\ntimed out after {policy.timeout_seconds}s".encode(),
                truncated=_trunc,
            )

        cancel_task.cancel()
        stdout_b, stderr_b, truncated = await comm_future
        return SandboxResult(
            returncode=proc.returncode, stdout=stdout_b, stderr=stderr_b, truncated=truncated,
        )
