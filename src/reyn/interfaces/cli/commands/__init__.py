"""Command registry for the reyn CLI.

Each command module exposes:
  register(sub)  — adds its subparser
  run(args)      — implementation, set as the func default
"""
from __future__ import annotations

from . import agent as agent
from . import audit as audit
from . import auth as auth
from . import chat as chat
from . import config as config
from . import cron as cron
from . import dogfood as dogfood
from . import embeddings as embeddings
from . import events as events
from . import init as init
from . import mcp as mcp
from . import memory as memory
from . import permissions as permissions
from . import pipe as pipe
from . import plugin as plugin
from . import run_once as run_once
from . import secret as secret
from . import source as source
from . import support_bundle as support_bundle
from . import topology as topology
from . import web as web

# Order is the order shown in `reyn --help`.
ALL = [init, config, run_once, chat, agent, topology, memory, permissions, auth, events, web, mcp, pipe, plugin, secret, source, cron, dogfood, embeddings, support_bundle, audit]
