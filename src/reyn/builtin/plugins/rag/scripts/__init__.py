"""Builtin ``rag`` plugin MCP server scripts (ADR 0064 P5, formerly
``reyn.builtin.mcp_servers``).

Each module here (``chunker_server.py`` / ``vector_store_server.py``) is
SELF-CONTAINED -- it imports nothing from ``reyn`` itself, only third-party
deps declared in the plugin's own ``requirements.txt`` (``chonkie`` /
``apsw`` / ``sqlite-vec`` / ``fastmcp``). This is what lets ``plugin_install``
copy the plugin directory verbatim and register each script's spawn
``command`` as-is (#3209 -- register-only; ``plugin_install`` never
provisions the venv), so a script runs fine via the operator/LLM's OWN venv
interpreter (``<venv>/bin/python <script path>``, created per the installing
skill's SETUP steps), with no dependency on reyn's own environment or on
this package being importable at all in the installed copy.
"""
from __future__ import annotations
