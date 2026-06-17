"""reyn.runtime — operational runtime concerns.

Domain grouping (broad-#5 regroup, issue #1682) for the packages governing
how a running OS instance is operated and constrained — distinct from the
execution core (``reyn.core``), persistence (``reyn.data``), surfaces
(``reyn.interfaces``), and dev tooling (``reyn.dev``):

- ``budget``  — cost / token budget tracking
- ``limits``  — runtime limit handling (iteration / resource caps)
- ``cron``    — scheduling primitives

Note: ``reyn.runtime.cron`` is the scheduling PACKAGE; the ``reyn.tools.cron``
module (the cron tool handler, which persists to ``.reyn/cron.yaml``) is a
separate, unmoved module.

Import-only node; exports nothing (pure path regroup, no surface change).
"""
