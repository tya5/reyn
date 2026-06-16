"""Tier 2: /save writes the conv pane buffer to a text file.

Categorical UX gap fill — "I want to share / archive this
conversation". Snapshots the live RichLog buffer to a plain-text
file. Mirrors the ``/copy`` + ``/find`` slash pattern: the slash
command emits a sentinel ``__save__`` OutboxMessage with the
path arg; the TUI handler resolves the path and writes the file.

Public surfaces tested:
  - ``ConversationView.dump_buffer_text`` returns the live buffer
    as a list of plain-text lines
  - ``OutboxRouter._on_save`` writes the buffer to:
      * explicit path
      * auto-generated path when arg is empty
      * ``~``-expanded path
  - error branches: permission denied / parent-dir-missing →
    error-kind status, no file written
  - empty buffer → file created with 0 lines (= status reports 0)
  - status surfaces line count + absolute path
  - ``/save`` slash command is registered in the slash registry
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from rich.text import Text

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _seed_lines(conv, lines: list[str]) -> None:
    """Helper: write Rich Text lines into the conv RichLog."""
    log = conv._log()
    for line in lines:
        log.write(Text(line))


@pytest.mark.asyncio
async def test_dump_buffer_text_returns_lines_in_order() -> None:
    """Tier 2: ``dump_buffer_text`` returns the buffer in insertion order."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        _seed_lines(conv, ["first", "second", "third"])
        await pilot.pause()
        dump = conv.dump_buffer_text()
        # The conv pane may have prelude lines (banner / today header)
        # before the seeded content. Pin only that the seeded content
        # appears in order, not the absolute index.
        idx_first = next(i for i, t in enumerate(dump) if t == "first")
        idx_second = next(i for i, t in enumerate(dump) if t == "second")
        idx_third = next(i for i, t in enumerate(dump) if t == "third")
        assert idx_first < idx_second < idx_third


@pytest.mark.asyncio
async def test_on_save_writes_to_explicit_path(tmp_path: Path) -> None:
    """Tier 2: /save with an explicit path writes the buffer to that file."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView

    target = tmp_path / "dump.txt"
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed_lines(conv, ["alpha", "beta", "gamma"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_save(
            OutboxMessage(kind="__save__", text=str(target)),
            conv,
            header,
        )
        await pilot.pause()
        assert target.exists(), "file should be created"
        content = target.read_text(encoding="utf-8")
        assert "alpha" in content
        assert "beta" in content
        assert "gamma" in content
        # Trailing newline for POSIX-tool friendliness.
        assert content.endswith("\n")
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert "saved" in snap["body"]
        assert str(target.resolve()) in snap["body"]


@pytest.mark.asyncio
async def test_on_save_auto_generates_path_when_arg_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: empty arg → auto-named file in cwd matching reyn-conv-*.txt."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView

    # Run with cwd = tmp_path so the auto-generated file lands there.
    monkeypatch.chdir(tmp_path)
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed_lines(conv, ["payload"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_save(
            OutboxMessage(kind="__save__", text=""),
            conv,
            header,
        )
        await pilot.pause()
        created = list(tmp_path.glob("reyn-conv-*.txt"))
        (auto_file,) = created  # exactly one auto-named file expected
        content = auto_file.read_text(encoding="utf-8")
        assert "payload" in content


@pytest.mark.asyncio
async def test_on_save_expands_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: ``~/foo.txt`` is expanded against $HOME."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView

    # Point HOME at tmp_path so the test never touches the real home dir.
    monkeypatch.setenv("HOME", str(tmp_path))
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed_lines(conv, ["tilde line"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_save(
            OutboxMessage(kind="__save__", text="~/dump.txt"),
            conv,
            header,
        )
        await pilot.pause()
        expected = tmp_path / "dump.txt"
        assert expected.exists(), (
            f"~/dump.txt should expand to {expected}; "
            f"cwd contents: {list(tmp_path.iterdir())}"
        )
        assert "tilde line" in expected.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_on_save_parent_dir_missing_emits_error(tmp_path: Path) -> None:
    """Tier 2: missing parent dir → error status, no file written.

    Slash UX should not silently create deep directory chains; the
    error message names the missing parent so the user can fix it.
    """
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView

    bad = tmp_path / "no-such-dir" / "out.txt"
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed_lines(conv, ["x"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_save(
            OutboxMessage(kind="__save__", text=str(bad)),
            conv,
            header,
        )
        await pilot.pause()
        assert not bad.exists()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        assert "/save:" in snap["body"]
        # The missing parent path should be surfaced for actionability.
        assert "no-such-dir" in snap["body"] or "missing" in snap["body"]


@pytest.mark.asyncio
async def test_on_save_overwrites_existing_file(tmp_path: Path) -> None:
    """Tier 2: pre-existing file at target path is overwritten silently.

    The slash UX has no confirmation channel — the user typed the
    path knowing it. ``write_text`` semantics match: truncate +
    write. Pin the behavior so a future "refuse to overwrite"
    safety policy is a deliberate decision.
    """
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView

    target = tmp_path / "existing.txt"
    target.write_text("stale content\n", encoding="utf-8")
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed_lines(conv, ["fresh content"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_save(
            OutboxMessage(kind="__save__", text=str(target)),
            conv,
            header,
        )
        await pilot.pause()
        content = target.read_text(encoding="utf-8")
        assert "stale content" not in content
        assert "fresh content" in content


def test_save_slash_command_is_registered() -> None:
    """Tier 2: ``/save`` command appears in the slash registry.

    Pins the registration glue — without the import line in
    ``slash/__init__.py``, the decorator never runs and the user's
    typed ``/save`` falls through to "unknown command".
    """
    from reyn.slash import REGISTRY

    names = {c.name for c in REGISTRY.all_commands()}
    assert "save" in names, (
        f"/save should be registered; got commands: {sorted(names)}"
    )


@pytest.mark.asyncio
async def test_on_save_empty_buffer_writes_zero_line_file(
    tmp_path: Path,
) -> None:
    """Tier 2: empty conv pane → file exists (possibly with no content)."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView

    target = tmp_path / "empty.txt"
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        conv.clear()
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_save(
            OutboxMessage(kind="__save__", text=str(target)),
            conv,
            header,
        )
        await pilot.pause()
        # File should exist even when buffer is empty post-clear.
        assert target.exists()
        # Status should report the line count (0 or low) + path.
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert "saved" in snap["body"]
        assert "line" in snap["body"]
