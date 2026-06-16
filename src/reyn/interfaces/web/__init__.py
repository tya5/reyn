"""reyn.interfaces.web — thin FastAPI + WebSocket gateway for the Reyn engine.

Wraps AgentRegistry, ChatSession, EventStore, and friends without modifying
the engine. The gateway is OS-adjacent infrastructure and must satisfy P7:
no skill-specific strings (phase names, artifact types, decision values)
may appear in this package. All engine data passes through as opaque JSON.
"""
