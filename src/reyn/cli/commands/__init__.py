"""Command registry for the reyn CLI.

Each command module exposes:
  register(sub)  — adds its subparser
  run(args)      — implementation, set as the func default
"""
from __future__ import annotations

from . import agent as agent
from . import chat as chat
from . import skills as skills
from . import config as config
from . import events as events
from . import eval as eval
from . import init as init
from . import lint as lint
from . import memory as memory
from . import permissions as permissions
from . import run as run
from . import topology as topology
from . import web as web

# Order is the order shown in `reyn --help`.
ALL = [init, config, skills, run, chat, agent, topology, eval, lint, memory, permissions, events, web]
