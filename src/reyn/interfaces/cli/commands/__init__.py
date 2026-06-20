"""Command registry for the reyn CLI.

Each command module exposes:
  register(sub)  — adds its subparser
  run(args)      — implementation, set as the func default
"""
from __future__ import annotations

from . import agent as agent
from . import auth as auth
from . import chainlit as chainlit
from . import chat as chat
from . import config as config
from . import cron as cron
from . import dogfood as dogfood
from . import embeddings as embeddings
from . import eval as eval
from . import events as events
from . import init as init
from . import lint as lint
from . import mcp as mcp
from . import memory as memory
from . import permissions as permissions
from . import run as run
from . import run_once as run_once
from . import secret as secret
from . import skill as skill
from . import skills as skills
from . import source as source
from . import support_bundle as support_bundle
from . import topology as topology
from . import web as web

# Order is the order shown in `reyn --help`.
ALL = [init, config, skills, skill, run, run_once, chat, agent, topology, eval, lint, memory, permissions, auth, events, web, chainlit, mcp, secret, source, cron, dogfood, embeddings, support_bundle]
