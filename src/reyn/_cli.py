"""Compatibility shim — keep `reyn._cli:main` working.

The CLI was reorganized into the `reyn.interfaces.cli` package. The pyproject.toml
entry point still references this module, so we re-export the public API.
"""
from .interfaces.cli import build_parser, main

__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    main()
