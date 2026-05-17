"""Cross-platform clipboard helper for the Reyn TUI.

Pulled out of ``app_outbox`` so the right-panel widgets can use it without
importing ``app_outbox`` (which itself imports widgets — would cycle).

The helper is platform-agnostic: it tries each known clipboard binary in
order and returns the label of the first one that succeeded. Users only
need ONE of them on PATH; the function never fails — empty
``tool_label`` simply means "no clipboard tool available; tell the user
to install pbcopy / xclip / wl-copy / xsel".
"""
from __future__ import annotations

# Order of clipboard tools we try. First match that succeeds wins.
# Each entry is (binary_name, argv_tail, label).
_CLIPBOARD_TOOLS: tuple[tuple[str, list[str], str], ...] = (
    ("pbcopy",   [],            "pbcopy"),                # macOS
    ("wl-copy",  [],            "wl-copy"),               # Wayland
    ("xclip",    ["-selection", "clipboard"], "xclip"),   # X11
    ("xsel",     ["--clipboard", "--input"], "xsel"),     # X11 fallback
    ("clip",     [],            "clip"),                  # Windows
)


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Pipe ``text`` to a platform clipboard tool. Returns ``(ok, tool_label)``.

    Looked up via ``shutil.which`` so the user only needs one of the
    binaries on PATH. We avoid hard-coding the OS because users may run,
    e.g., xclip inside a Linux VM regardless of the host platform.

    BLOCKING: ``subprocess.run`` is synchronous and can hold the calling
    thread for up to ~2 s (timeout). Callers inside an async event loop
    should use :func:`copy_to_clipboard_async` instead.
    """
    import shutil
    import subprocess

    for binary, tail, label in _CLIPBOARD_TOOLS:
        path = shutil.which(binary)
        if path is None:
            continue
        try:
            subprocess.run(
                [path, *tail],
                input=text.encode("utf-8"),
                check=True,
                timeout=2.0,
            )
            return True, label
        except Exception:
            continue
    return False, ""


async def copy_to_clipboard_async(text: str) -> tuple[bool, str]:
    """Async variant — off-loads :func:`copy_to_clipboard` to a thread executor.

    The blocking subprocess (timeout=2 s) runs on the default executor so
    the event loop stays free to drain other outbox events (streaming
    chunks, status messages, traces). Without this off-load, a single
    ``/copy`` could freeze the TUI for up to 2 seconds per clipboard
    tool attempted.

    Returns the same ``(ok, tool_label)`` shape as the sync version.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, copy_to_clipboard, text)


__all__ = ["copy_to_clipboard", "copy_to_clipboard_async"]
