"""reyn × Chainlit integration (PoC).

Surface: ``reyn chainlit`` launches Chainlit's dev server which loads
``app.py`` in this package. Coexists with ``reyn chat`` (TUI) and
``reyn web`` (FastAPI + openui) — all three share the same
``Session`` / `OutboxMessage` model defined in ``reyn.runtime``.

Module layout:
- ``adapter``: pure function mapping ``OutboxMessage`` → Chainlit payload.
  No chainlit import, so unit tests run without the ``[chainlit]`` extra.
- ``app``: Chainlit-side glue (@cl.on_chat_start / @cl.on_message).
  Imports chainlit at module level — only loaded by ``chainlit run``.
"""
