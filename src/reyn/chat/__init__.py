"""Chat agent layer.

Session runs a long-lived asyncio loop that turns user utterances into
implicit skill invocations via the stdlib `skill_router` skill. Skills run
concurrently; the user can keep chatting while they execute.
"""
from .session import ChatMessage, Session

__all__ = ["Session", "ChatMessage"]
