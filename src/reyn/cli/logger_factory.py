"""Logger selection — single place that knows about Rich vs plain."""
from __future__ import annotations


def make_logger(rich: bool = False, **opts):
    if rich:
        from reyn.reporters.rich import RichLogger
        return RichLogger(**opts)
    from reyn.reporters.console import ConsoleLogger
    return ConsoleLogger(**opts)
