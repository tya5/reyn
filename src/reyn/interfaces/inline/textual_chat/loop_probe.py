"""Event-loop responsiveness instrumentation for the inline CUI (#3539).

The tripwire, its durable record, the stack-dump arm and the tick loop are
:mod:`reyn.runtime.loop_tripwire` since #5898 (``reyn:web``'s loop needed the
same watcher, with the shipped config — see that module's own docstring for
the two-layer design, the ``REYN_PROF_DUMP`` detail probe, the environment
axes, and the measured baseline). This module is the CUI-facing name for the
same objects: ``TextualChatApp._watch_loop_responsiveness`` reads them from
here, and adds what only a CUI has — the chrome banner, the message-pump
heartbeat window and the key-arrival count that ride along in its notices.
"""
from __future__ import annotations

from reyn.runtime.loop_tripwire import (
    _DUMP_ENV,
    _RECORD_INTERVAL_S,
    _TICK_SECONDS,
    _TRIPWIRE_MS,
    LoopTripwire,
    StallDumpArm,
    dump_path,
    environment_axes,
    stall_banner,
    stall_dump_path,
    stall_log_line,
    stall_recovered_log_line,
    watch_event_loop,
    write_record,
)

__all__ = [
    "_DUMP_ENV",
    "_RECORD_INTERVAL_S",
    "_TICK_SECONDS",
    "_TRIPWIRE_MS",
    "LoopTripwire",
    "StallDumpArm",
    "dump_path",
    "environment_axes",
    "stall_banner",
    "stall_dump_path",
    "stall_log_line",
    "stall_recovered_log_line",
    "watch_event_loop",
    "write_record",
]
