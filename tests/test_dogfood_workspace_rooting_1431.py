"""Tier 2: #1431 — dogfood roots the chat workspace on the container repo.

dogfood builds an EnvironmentBackend (DockerEnvironmentBackend for a container
agent) and threads it to the chat session as ``environment_backend`` — but it
passed ``workspace_base_dir=None``, so chat file ops (file__read/grep/glob/edit)
rooted on the HOST cwd while the exec/diff seam pointed at the container repo.
That FS disagreement is the #187 wrong-FS class (#1410 base_dir sibling); the
#1402 single-source migration made the dropped workspace dirs explicit and
visible. Fix: thread the env-backend's PARTNER container repo root (``_wb``) +
host-side state dir (``_ws``) from the CLI entry through ``_build_live_runner``
into the session construction.

This is a construction-wiring invariant (the fix is a thread, and the downstream
workspace_base_dir -> OpContext FS root is already covered by the chat.py path).
Falsifiable: revert any link of the thread → this fails, naming the gap.
"""
from __future__ import annotations

import ast
from pathlib import Path

_DOGFOOD = (
    Path(__file__).resolve().parents[1]
    / "src" / "reyn" / "interfaces" / "cli" / "commands" / "dogfood.py"
)


def _tree() -> ast.AST:
    return ast.parse(_DOGFOOD.read_text(encoding="utf-8"))


def _kw_value(call: ast.Call, name: str) -> ast.AST | None:
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def test_factory_threads_workspace_dirs_not_none() -> None:
    """Tier 2: #1431 — dogfood's build_scoped_chat_session call passes
    workspace_base_dir / workspace_state_dir as the threaded ws_base_dir /
    ws_state_dir names, NOT ``None`` (which rooted file ops on the host cwd =
    the #187 wrong-FS bug)."""
    calls = [
        n for n in ast.walk(_tree())
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "build_scoped_chat_session"
    ]
    assert calls, "no build_scoped_chat_session(...) call in dogfood.py"
    call = calls[0]
    base = _kw_value(call, "workspace_base_dir")
    state = _kw_value(call, "workspace_state_dir")
    assert isinstance(base, ast.Name) and base.id == "ws_base_dir", (
        "dogfood must pass workspace_base_dir=ws_base_dir (the container repo "
        "root), not None — else chat file ops root on the host cwd (#187 wrong-FS)"
    )
    assert isinstance(state, ast.Name) and state.id == "ws_state_dir"


def test_cli_passes_env_backend_workspace_dirs_to_runner() -> None:
    """Tier 2: #1431 — the dogfood CLI entry threads the env-backend's _wb/_ws
    into _build_live_runner (so the session factory closure can root the
    workspace on the container repo). Pins the full thread, not just the leaf."""
    runner_calls = [
        n for n in ast.walk(_tree())
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_build_live_runner"
    ]
    assert runner_calls, "no _build_live_runner(...) call in dogfood.py"
    # At least one call threads both ws_base_dir and ws_state_dir from _wb/_ws.
    def _threads(c: ast.Call) -> bool:
        wb = _kw_value(c, "ws_base_dir")
        ws = _kw_value(c, "ws_state_dir")
        return (
            isinstance(wb, ast.Name) and wb.id == "_wb"
            and isinstance(ws, ast.Name) and ws.id == "_ws"
        )
    assert any(_threads(c) for c in runner_calls), (
        "the dogfood CLI must call _build_live_runner(..., ws_base_dir=_wb, "
        "ws_state_dir=_ws) so the workspace is rooted on the container repo"
    )
