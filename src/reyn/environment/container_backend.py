"""DockerEnvironmentBackend — repo FS + exec INSIDE a Docker container (FP-0008 #1115 Stage 2).

One class implementing BOTH Protocols (案C-pure):
  - :class:`~reyn.environment.backend.EnvironmentBackend` — repo filesystem ops
    run *inside* the container against ``repo_dir`` (e.g. ``/testbed``);
  - :class:`~reyn.sandbox.backend.SandboxBackend` — ``run()`` exec inside the
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

Axis-agnostic / P7-clean: no skill / phase / artifact strings; bound to a
``(container, repo_dir)`` pair. FS uses a sync runner (the EnvironmentBackend
Protocol is sync, matching Workspace); ``run()`` uses an async runner (the
SandboxBackend Protocol is async). Both are injectable so the orchestration is
unit-testable without a live Docker daemon.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable, Pattern

from reyn.environment.backend import GrepResult
from reyn.sandbox.backend import SandboxResult
from reyn.sandbox.policy import SandboxPolicy

# Sync runner: execute argv (optionally stdin), return SandboxResult. Injected so
# the FS-op orchestration is testable without Docker; default = _sync_runner.
SyncRunner = Callable[..., SandboxResult]
# Async runner: same contract for run() (mirrors the PR-A backend runner).
AsyncRunner = Callable[..., Awaitable[SandboxResult]]


def _sync_runner(
    argv: list[str], *, stdin: bytes | None = None, timeout: int | None = None
) -> SandboxResult:
    """Real sync runner: spawn argv via ``subprocess.run`` and capture output."""
    try:
        completed = subprocess.run(
            argv, input=stdin, capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return SandboxResult(
            returncode=-1, stdout=b"", stderr=f"timed out after {timeout}s".encode(),
        )
    return SandboxResult(
        returncode=completed.returncode, stdout=completed.stdout or b"", stderr=completed.stderr or b"",
    )


async def _async_runner(
    argv: list[str], *, stdin: bytes | None = None, timeout: int | None = None
) -> SandboxResult:
    """Real async runner for run() (mirrors PR-A's _subprocess_runner)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return SandboxResult(returncode=-1, stdout=b"", stderr=f"timed out after {timeout}s".encode())
    return SandboxResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out or b"", stderr=err or b"",
    )


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


class DockerEnvironmentBackend:
    """Repo FS + exec inside a Docker container (dual-Protocol, bridge-free)."""

    name: str = "docker"

    def __init__(
        self,
        *,
        container: str,
        repo_dir: str,
        docker_bin: str = "docker",
        python_bin: str = "python3",
        fs_runner: SyncRunner | None = None,
        runner: AsyncRunner | None = None,
    ) -> None:
        self.container = container
        self.repo_dir = repo_dir
        self.docker_bin = docker_bin
        self.python_bin = python_bin
        self._fs_runner: SyncRunner = fs_runner or _sync_runner
        self._runner: AsyncRunner = runner or _async_runner

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

    async def run(
        self, argv: list[str], policy: SandboxPolicy, *, stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        """``docker exec`` of argv (via a login shell) with cwd=repo_dir — NO host-diff bridge.

        The files are already in ``repo_dir`` (the agent edited them via the FS
        methods above), so there is nothing to sync in. Honors only
        ``policy.timeout_seconds`` (the fidelity boundary, as in PR-A).

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
        exec_argv = [
            self.docker_bin, "exec", *(["-i"] if stdin is not None else []),
            "-w", self.repo_dir, self.container,
            "bash", "-lc", 'exec "$@"', "reyn-exec", *argv,
        ]
        return await self._runner(exec_argv, stdin=stdin, timeout=policy.timeout_seconds)
