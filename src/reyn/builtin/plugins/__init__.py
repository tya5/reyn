"""``src/reyn/builtin/plugins/`` — reyn's own shipped plugins (ADR 0064 §3.1/§3.8).

The ``{kind: "builtin"}`` variant of ``plugin_install``'s discriminated
``source`` (``reyn.core.op_runtime.plugin_install._builtin_plugin_dir``)
resolves a name to ``<this dir>/<name>/`` — a self-contained plugin
directory (``.reyn-plugin/plugin.json`` + optional ``mcp``/``pipelines``/
``skills`` subdirs, same shape as any other plugin source).

Empty for now — P2 ships the install MACHINERY only. ADR 0064 §3.1 frames
"builtin RAG becomes the first plugin (dogfood)" as a follow-up once this
directory exists to receive it; adding that plugin is out of scope here.
"""
from __future__ import annotations
