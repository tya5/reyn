"""`reyn web` — Web UI ゲートウェイサーバを起動する。

FastAPI + WebSocket ゲートウェイ (reyn.web.server) を uvicorn で起動します。
フロントエンドを http://localhost:<port> から利用できます。

このコマンドは reyn の `[web]` オプション依存を必要とします:
    pip install -e ".[web]"
"""
from __future__ import annotations

import argparse
import sys


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
    p.set_defaults(func=run)


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

    uvicorn.run(
        "reyn.web.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
