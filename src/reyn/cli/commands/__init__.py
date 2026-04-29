"""Command registry for the reyn CLI.

Each command module exposes:
  register(sub)  — adds its subparser
  run(args)      — implementation, set as the func default
"""
from __future__ import annotations

from . import skills as skills
from . import config as config
from . import events as events
from . import eval as eval
from . import format as format
from . import init as init
from . import lint as lint
from . import run as run

# Order is the order shown in `reyn --help`.
ALL = [init, config, skills, run, eval, lint, format, events]
