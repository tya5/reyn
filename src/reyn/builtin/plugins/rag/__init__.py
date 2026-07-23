"""``src/reyn/builtin/plugins/rag/`` -- the builtin ``rag`` plugin (ADR 0064 P5).

Not imported by reyn core at runtime -- ``plugin_install`` resolves this
directory by PATH (``reyn.core.op_runtime.plugin_install._builtin_plugin_dir``),
copies it to ``~/.reyn/plugins/rag/``, and never imports it as Python. This
``__init__.py`` (and the one in ``scripts/``) exists ONLY so this repo's own
tests can import ``reyn.builtin.plugins.rag.scripts.<module>`` directly for
unit coverage of the pure functions the MCP servers wrap, without going
through a real per-plugin venv (#3209 -- the operator/LLM's own venv,
created per the installing skill's SETUP steps, not one reyn provisions).
"""
from __future__ import annotations
