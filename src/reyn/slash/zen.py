"""/zen — The Zen of Reyn.

Inspired by `import this` (Tim Peters' Zen of Python). Twenty aphorisms
distilling Reyn's design philosophy: constraint over autonomy,
predictability over cleverness, the Workspace and Event log as the only
two sources of truth.

Bilingual on purpose — Reyn is a Japanese-originated framework that
serves both languages first-class, and the Zen reflects that.
"""
from __future__ import annotations

from reyn.chat.slash import reply, slash

_ZEN = """The Zen of Reyn, by Tetsuya Yasuda

  Constraint is the path to creativity.
  Predictability beats cleverness.
  The Phase does not know its successor.
  Skills come and go; the OS abides.
  The Workspace is the single source of truth.
  The Event log is the audit truth.
  State that mutates without an event did not happen.
  An OS that names a Skill has already failed it.
  The LLM decides; the OS determines.
  Permission is enforced, never asked.
  Three decisions suffice: continue, finish, abort.
  "Revise" is a Skill's word — keep it out of the OS.
  When in doubt, write to the Workspace.
  When in greater doubt, emit an event.
  A new Skill should never demand a new OS flag.
  Add a Phase, not a special case.
  Validate at the boundary; trust within.
  Constraints are not handcuffs — they are guardrails.
  小さな契約を守れば、大きな自動化が立ち上がる。
  Workspace に書かれていないものは、未来に届かない。"""


@slash("zen", summary="The Zen of Reyn", hidden=True)
async def zen_cmd(session: "object", args: str) -> None:
    await reply(session, _ZEN)
