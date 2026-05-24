"""`reyn chainlit` — Chainlit ベースの Web チャット UI を起動する (PoC)。

Chainlit (https://chainlit.io) を `python -m chainlit run` 経由で起動し、
``src/reyn/chainlit_app/app.py`` に定義した @cl.on_chat_start /
@cl.on_message ハンドラ越しに `ChatSession` と接続する。

TUI (`reyn chat`) / FastAPI Web UI (`reyn web`) と共存する第三の surface:
- 既存 `reyn web` (FP-0013 MessageBus + openui) と同じ "サーバ起動" 体験
- 各 Chainlit ブラウザセッション = 独立の `ChatSession`
- multi-user は Chainlit 側 WebSocket セッション分離に乗る

このコマンドは reyn の `[chainlit]` オプション依存を必要とする:
    pip install -e ".[chainlit]"
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def register(sub) -> None:
    p = sub.add_parser(
        "chainlit",
        help="Chainlit Web チャット UI を起動する (PoC)",
        description=(
            "Chainlit を起動して http://<host>:<port>/ から reyn にチャットする。\n"
            "インストール: pip install -e \".[chainlit]\""
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
        default=8000,
        metavar="PORT",
        help="バインドするポート (デフォルト: 8000 = chainlit default)",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help="コード変更時に自動リロードする (= chainlit --watch、 開発用)",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="起動時にブラウザを開かない (= chainlit --headless)",
    )
    p.set_defaults(func=run)


def _app_module_path() -> Path:
    """Resolve ``src/reyn/chainlit_app/app.py`` for the running install."""
    return Path(__file__).resolve().parents[2] / "chainlit_app" / "app.py"


def run(args: argparse.Namespace) -> None:
    try:
        import chainlit  # noqa: F401
    except ImportError:
        print(
            "Error: chainlit is not installed. "
            "Run `pip install -e \".[chainlit]\"` to install Chainlit.",
            file=sys.stderr,
        )
        sys.exit(1)

    app_path = _app_module_path()
    if not app_path.is_file():
        print(
            f"Error: reyn chainlit app entry not found at {app_path}. "
            "Reinstall reyn or report a bug.",
            file=sys.stderr,
        )
        sys.exit(1)

    # First-run UX: drop a reyn-branded chainlit.md into cwd so the
    # operator sees reyn context (not chainlit's generic boilerplate)
    # the first time they hit the URL. Idempotent — operator edits
    # are preserved on subsequent launches.
    from reyn.chainlit_app.first_run import ensure_all_assets
    ensure_all_assets(Path.cwd())

    cmd: list[str] = [
        sys.executable,
        "-m",
        "chainlit",
        "run",
        str(app_path),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.watch:
        cmd.append("--watch")
    if args.headless:
        cmd.append("--headless")

    env = os.environ.copy()
    try:
        subprocess.run(cmd, env=env, check=False)
    except KeyboardInterrupt:
        sys.exit(0)
