"""Environment backends (FP-0008 #1115) — repo-filesystem mechanism abstraction.

``EnvironmentBackend`` decouples the Workspace from the concrete filesystem the
repo working tree lives on. ``HostBackend`` (Stage 1) is the identity over the
local Python filesystem; a container backend (Stage 2) lets the repo FS live in
a container while the OS + permission layer stay host-side.
"""
from reyn.environment.backend import EnvironmentBackend, GrepResult
from reyn.environment.host_backend import HostBackend

__all__ = ["EnvironmentBackend", "GrepResult", "HostBackend"]
