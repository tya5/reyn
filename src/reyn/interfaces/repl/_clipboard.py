"""Cross-platform clipboard helper for the inline CUI (`/copy`) and copy mode.

Thin wrapper over `pyperclip <https://pypi.org/project/pyperclip/>`_ (#3616 ①)
rather than a hand-rolled per-OS dispatch table. The previous version
re-implemented pyperclip's own job — probing ``pbcopy`` / ``wl-copy`` /
``xclip`` / ``xsel`` / ``clip`` via ``shutil.which`` and shelling out with
``subprocess`` — and its own Windows arm piped UTF-8 bytes to ``clip.exe``,
which decodes stdin under the OS's OEM code page (CP932 for Japanese, etc.)
rather than UTF-8, garbling any non-ASCII text. pyperclip's Windows backend
writes ``CF_UNICODETEXT`` directly, so ``clip.exe`` and code-page
interpretation never enter the path. The fix closes the class (reyn owning
clipboard-platform differences) rather than patching the Windows arm alone.

``copy_to_clipboard`` never raises: pyperclip raises ``PyperclipException``
when no backend is available (or a backend call fails), and that — like any
other exception a backend might surface — is caught here and turned into
``False``, so a failed copy stays observable to the caller instead of
propagating or silently reading as success.

pyperclip has no public, stable "which backend won" API (only
``is_available()`` and a private module-level function reference), so the
former ``(ok, tool_label)`` contract's label half is gone: callers get a
plain ``bool``. Every caller of the old label was audited and updated
(#3616 ①) — see ``_copy_sentinel.py`` and ``textual_chat/app.py``.
"""
from __future__ import annotations


def copy_to_clipboard(text: str) -> bool:
    """Copy ``text`` to the system clipboard via pyperclip. Returns whether it
    succeeded.

    BLOCKING: pyperclip's macOS/Linux backends shell out to a platform tool
    (``pbcopy``/``xclip``/``wl-copy``/``xsel``) via a synchronous
    ``subprocess.Popen(...).communicate()``. Callers inside an async event
    loop should use :func:`copy_to_clipboard_async` instead.
    """
    import pyperclip

    try:
        pyperclip.copy(text)
        return True
    except Exception:
        return False


async def copy_to_clipboard_async(text: str) -> bool:
    """Async variant — off-loads :func:`copy_to_clipboard` to a thread executor.

    pyperclip's macOS/Linux backends shell out to a subprocess; off-loading
    keeps the event loop free to drain other outbox events (streaming
    chunks, status messages, traces) while that subprocess runs. Without
    this off-load, a single ``/copy`` or copy-mode yank could freeze the TUI
    for as long as the subprocess takes.

    Returns the same ``bool`` shape as the sync version.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, copy_to_clipboard, text)


__all__ = ["copy_to_clipboard", "copy_to_clipboard_async"]
