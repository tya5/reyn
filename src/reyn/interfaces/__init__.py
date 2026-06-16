"""``interfaces/`` — user/external interface subsystem group (#1682 #5 partial).

Groups the interface layers under one domain dir to cut top-level over-flat:
``cli/`` (terminal CLI), ``tui/`` (Textual TUI), ``api/`` + ``web/`` (HTTP/A2A),
``chainlit_app/`` (chainlit surface). NOT ``slash/`` (debatable, deferred).

The ``reyn`` console-script (pyproject ``reyn._cli:main``) is unchanged —
``_cli.py`` stays at the package root; only its relative import of the moved
``cli/`` package is repointed.
"""
