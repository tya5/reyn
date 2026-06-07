"""`reyn web` — Web UI ゲートウェイサーバを起動する。

FastAPI + WebSocket ゲートウェイ (reyn.web.server) を uvicorn で起動します。
フロントエンドを http://localhost:<port> から利用できます。

このコマンドは reyn の `[web]` オプション依存を必要とします:
    pip install -e ".[web]"
"""
from __future__ import annotations

import argparse
import sys

from reyn.cli.env_backend import build_environment_backend, register_env_backend_args


def register(sub) -> None:
    p = sub.add_parser(
        "web",
        help="Web UI ゲートウェイサーバを起動する",
        description=(
            "FastAPI + WebSocket ゲートウェイを uvicorn で起動します。\n"
            "インストール: pip install -e \".[web]\""
        ),
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        metavar="HOST",
        help="バインドするホスト (デフォルト: 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8080,
        metavar="PORT",
        help="バインドするポート番号 (デフォルト: 8080)",
    )
    p.add_argument(
        "--reload",
        action="store_true",
        help="コード変更時に自動リロードする (開発用)",
    )
    p.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        dest="log_level",
        metavar="LEVEL",
        help="uvicorn のログレベル (デフォルト: info)",
    )
    p.add_argument(
        "--default-design",
        default=None,
        metavar="SLUG",
        dest="default_design",
        help="デフォルトの design slug (env REYN_WEB_DEFAULT_DESIGN に設定)",
    )
    # Parity with `reyn chat --eager-embedding-build` (= B25-S5-1).
    # Builds the action-index synchronously on session start so
    # ``search_actions`` is visible in tools[] from the first router
    # turn, instead of becoming visible only after the background build
    # finishes. Paid as a one-time per-session cost; ``is_ready()`` then
    # short-circuits subsequent turns via the SQLite cache.
    p.add_argument(
        "--eager-embedding-build",
        action="store_true",
        default=False,
        dest="eager_embedding_build",
        help=(
            "action_embedding_index を session 起動時 sync で build "
            "(env REYN_WEB_EAGER_EMBEDDING_BUILD=1 と同等)。"
            " search_actions を 1 turn 目から見せたい dogfood / web "
            "デプロイで有効化。"
        ),
    )
    # #1401: scoped capabilities for the A2A server path — symmetric with
    # `reyn chat`. Lets a headless `reyn web` (the faithful SWE-eval A2A host)
    # run agent file/exec ops in a per-instance container, scope out tools (e.g.
    # web for faithful eval), and grant repo file-write. Threaded as the
    # env-backend INSTANCE (no env-var rebuild) via web/deps; see run().
    register_env_backend_args(p)  # --env-backend / --container
    p.add_argument(
        "--grant-file-write",
        action="store_true",
        default=False,
        dest="grant_file_write",
        help=(
            "agent に file.read/write を resolver 層で許可 "
            "(sandbox write_paths ∩ env-backend repo zone で bound)。"
            " headless/scripted SWE で repo を編集させる場合に有効化 "
            "(`reyn chat --grant-file-write` と同等)。"
        ),
    )
    p.add_argument(
        "--exclude-tools",
        dest="exclude_tools",
        default=None,
        metavar="NAMES",
        help=(
            "LLM の visible catalog から隠すツール名 (カンマ区切り、例 "
            "web__search,web__fetch)。faithful eval の web-leak 抑止に "
            "(`reyn chat --exclude-tools` と同等)。"
        ),
    )
    p.set_defaults(func=run)


def _apply_cli_scoped_overrides(args: argparse.Namespace) -> None:
    """#1401: build the env-backend INSTANCE (CLI process) + thread it, the
    workspace dirs, exclude-tools, and the file-write grant to the A2A session
    factory via web/deps' module-global holder. No-op when no scoped flag is
    set (a plain `reyn web` stays byte-identical).

    Detects scoped intent from the args BEFORE ``build_environment_backend``
    (which may LAUNCH a container) so the --reload guard fires without side
    effects. --reload spawns a worker subprocess the parent's module-global
    cannot reach (silent no-op) → fail loud instead.
    """
    grant = bool(getattr(args, "grant_file_write", False))
    exclude_raw = getattr(args, "exclude_tools", None)
    scoped_intent = (
        getattr(args, "env_backend", "host") != "host"
        or grant
        or bool(exclude_raw)
    )
    if not scoped_intent:
        return
    if getattr(args, "reload", False):
        print(
            "Error: --reload is incompatible with the scoped capability flags "
            "(--env-backend=docker / --grant-file-write / --exclude-tools). The "
            "uvicorn reload worker is a subprocess the CLI-set overrides cannot "
            "reach (silent no-op). Run without --reload for a scoped server.",
            file=sys.stderr,
        )
        sys.exit(2)

    from reyn.web import deps as _web_deps

    env_backend, wb, ws, env_cleanup = build_environment_backend(args)
    if env_cleanup is not None:
        import atexit
        atexit.register(env_cleanup)
    exclude_tools = frozenset(
        t.strip() for t in (exclude_raw or "").split(",") if t.strip()
    ) or None
    _web_deps.set_cli_scoped_overrides(
        _web_deps.CliScopedOverrides(
            environment_backend=env_backend,
            workspace_base_dir=wb,
            workspace_state_dir=ws,
            exclude_tools=exclude_tools,
            grant_file_write=grant,
        )
    )


def run(args: argparse.Namespace) -> None:
    try:
        import uvicorn
    except ImportError:
        print(
            "Error: uvicorn is not installed. "
            "Run `pip install -e \".[web]\"` to install the web gateway dependencies.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        import fastapi  # noqa: F401
    except ImportError:
        print(
            "Error: fastapi is not installed. "
            "Run `pip install -e \".[web]\"` to install the web gateway dependencies.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --default-design flag: propagate via env so web_config router can read it.
    import os
    if getattr(args, "default_design", None):
        os.environ["REYN_WEB_DEFAULT_DESIGN"] = args.default_design

    # --eager-embedding-build flag: propagate via env so the web session
    # factory (= reyn.web.deps:_session_factory) can read it. Parity with
    # `reyn chat --eager-embedding-build` (= B25-S5-1).
    if getattr(args, "eager_embedding_build", False):
        os.environ["REYN_WEB_EAGER_EMBEDDING_BUILD"] = "1"

    # #1401: thread the scoped capabilities to the A2A server path (before
    # uvicorn.run so the lazy session factory / perm resolver read them).
    _apply_cli_scoped_overrides(args)

    uvicorn.run(
        "reyn.web.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
