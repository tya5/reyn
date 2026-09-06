"""#5851 stage (a) — the process-wide memory footprint reader + guard.

Owner-hit incident (2026-09-06 night): a single reyn process grew
unboundedly and took the host down with it, no signal anywhere in the
audit trail beforehand. This module is the OBSERVATION half only — no
halt (stage (b)/(c), a separate PR each per lead-coder's dispatch order).

Architect ruling (#5851, real-machine measurement on the owner's own
host, `origin/main` `4fd5119e0`):

- **metric**: darwin = ``phys_footprint`` (``libproc.proc_pid_rusage``,
  ``RUSAGE_INFO_V2`` — the same figure Activity Monitor's "Memory" column
  and the ``footprint(1)`` CLI report; SHRINKS on free, unlike
  ``ru_maxrss`` which is a peak that never comes back down). linux =
  ``rss`` (``/proc/self/statm``'s resident-page count × page size).
  Measured cost: ~39µs on darwin, no fork, no subprocess.
- **never fabricate a value on an unsupported platform** — ``None``, and
  the caller (session.py) emits ``process_footprint_unavailable`` once.
  An observation must name its own referent (this repo's own
  verification-hazards discipline): the audit-event and the status
  snapshot both carry ``metric`` alongside every ``bytes`` value, never
  a bare number a reader has to guess the meaning of.
- **the reader is injectable** — ``Callable[[], int | None]``, so a test
  drives this seam with a plain function, never a real allocation, a
  ``sleep``, or a timer.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

# A per-call reader: returns the current measured footprint in bytes, or
# None if this platform (or this specific read) cannot produce one.
ProcessMemoryReader = Callable[[], "int | None"]


def process_memory_metric_name() -> "str | None":
    """The metric name THIS platform's reader measures, or ``None`` if no
    reader exists for it (#5851 architect ruling — see module docstring).

    Pure ``sys.platform`` check, no syscall — config validation (#5851 ③:
    ``enforce: true`` with a reader-less platform is a load-time error)
    calls this directly rather than invoking the reader itself, which
    would mean a config-load path performs a live measurement as a side
    effect."""
    if sys.platform == "darwin":
        return "phys_footprint"
    if sys.platform.startswith("linux"):
        return "rss"
    return None


def _read_darwin_phys_footprint() -> "int | None":
    """``libproc_proc_pid_rusage(getpid(), RUSAGE_INFO_V2, &info)`` via
    ctypes — no ``psutil`` dependency, no fork/subprocess (a ``ps`` call
    would itself spawn a process, which #5851's own ruling names as
    exactly the harm to avoid when memory is already under pressure).
    ``ri_phys_footprint`` is the field; see module docstring for why it,
    not ``ri_resident_size`` (RSS) or ``ru_maxrss`` (a non-shrinking
    peak, unusable for "did this actually shrink after eviction")."""
    import ctypes
    import ctypes.util
    import os

    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError:
        return None

    # RUSAGE_INFO_V2 layout (sys/resource.h): a 16-byte UUID, then a run
    # of uint64_t counters. ri_phys_footprint is the 9th field (index 8,
    # 0-based, counting ri_uuid as field 0) in the V2 struct — only the
    # fields up to and including it
    # are declared; ctypes ignores any real struct fields beyond what's
    # named here (the kernel writes a V2-sized struct; nothing this code
    # reads depends on the layout continuing beyond ri_phys_footprint).
    class _RUsageInfoV2(ctypes.Structure):
        _fields_ = [
            ("ri_uuid", ctypes.c_uint8 * 16),
            ("ri_user_time", ctypes.c_uint64),
            ("ri_system_time", ctypes.c_uint64),
            ("ri_pkg_idle_wkups", ctypes.c_uint64),
            ("ri_interrupt_wkups", ctypes.c_uint64),
            ("ri_pageins", ctypes.c_uint64),
            ("ri_wired_size", ctypes.c_uint64),
            ("ri_resident_size", ctypes.c_uint64),
            ("ri_phys_footprint", ctypes.c_uint64),
            ("ri_proc_start_abstime", ctypes.c_uint64),
            ("ri_proc_exit_abstime", ctypes.c_uint64),
            ("ri_child_user_time", ctypes.c_uint64),
            ("ri_child_system_time", ctypes.c_uint64),
            ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
            ("ri_child_interrupt_wkups", ctypes.c_uint64),
            ("ri_child_pageins", ctypes.c_uint64),
            ("ri_child_elapsed_abstime", ctypes.c_uint64),
            ("ri_diskio_bytesread", ctypes.c_uint64),
            ("ri_diskio_byteswritten", ctypes.c_uint64),
            ("ri_cpu_time_qos_default", ctypes.c_uint64),
            ("ri_cpu_time_qos_maintenance", ctypes.c_uint64),
            ("ri_cpu_time_qos_background", ctypes.c_uint64),
            ("ri_cpu_time_qos_utility", ctypes.c_uint64),
            ("ri_cpu_time_qos_legacy", ctypes.c_uint64),
            ("ri_cpu_time_qos_user_initiated", ctypes.c_uint64),
            ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
            ("ri_billed_system_time", ctypes.c_uint64),
            ("ri_serviced_system_time", ctypes.c_uint64),
        ]

    RUSAGE_INFO_V2 = 2
    libc.proc_pid_rusage.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
    ]
    libc.proc_pid_rusage.restype = ctypes.c_int
    info = _RUsageInfoV2()
    # `rusage_info_t` is `void *` in <libproc.h> — the real parameter type
    # is `void **`, not `struct rusage_info_v2 *`; the struct memory is
    # reached by casting the SAME address, not by declaring the struct
    # pointer type directly (a direct `POINTER(_RUsageInfoV2)` cast here
    # crashed — confirmed by measurement, not guessed).
    info_ptr = ctypes.cast(ctypes.byref(info), ctypes.POINTER(ctypes.c_void_p))
    rv = libc.proc_pid_rusage(os.getpid(), RUSAGE_INFO_V2, info_ptr)
    if rv != 0:
        return None
    return int(info.ri_phys_footprint)


def _read_linux_rss() -> "int | None":
    """``/proc/self/statm``'s 2nd field (resident, in pages) × the page
    size — no ``ps``/``pmap`` subprocess, matching the darwin reader's
    own no-fork constraint."""
    import os

    try:
        with open("/proc/self/statm", "r", encoding="ascii") as f:
            fields = f.read().split()
        resident_pages = int(fields[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError, AttributeError):
        return None
    return resident_pages * page_size


def make_process_memory_reader() -> ProcessMemoryReader:
    """The real, platform-selected reader — production's own default
    (``ProcessMemoryGuard``'s default when no reader is injected). A test
    never calls this; it injects its own ``Callable[[], int | None]``
    directly into ``ProcessMemoryGuard.reader``."""
    metric = process_memory_metric_name()
    if metric == "phys_footprint":
        return _read_darwin_phys_footprint
    if metric == "rss":
        return _read_linux_rss
    return lambda: None


@dataclass
class ProcessMemoryGuard:
    """#5851 ②: ONE instance per process — created at registry bootstrap
    (``factory_config.py``'s ``SessionFactoryConfig.from_config``, called
    once per frontend-process bootstrap) and threaded to every ``Session``
    the SAME route ``history_resident_config`` already uses. A process
    hosting N agent sessions shares ONE guard because the cap this
    eventually enforces (stage (c)) is a property of the PROCESS, not of
    any one session — ``cap_bytes``/``enforce`` are process-wide, not
    per-agent.

    Stage (a): read-only. No halt latch — that lands in stage (c), once
    owner judgment 1/3 (halt-vs-compact, non-interactive behaviour) is
    made. This dataclass exists now so stage (c) adds a latch FIELD here
    rather than re-threading the whole construction route again.
    """

    reader: ProcessMemoryReader = field(default_factory=make_process_memory_reader)
    metric: "str | None" = field(default_factory=process_memory_metric_name)
    cap_bytes: "int | None" = None
    enforce: bool = False
    # #5851 ⑤: process_footprint_unavailable fires "起動時1回" — process-
    # scoped (this guard IS the process, by construction: one instance,
    # shared by every session's factory_config), not per-session, so a
    # 2nd/3rd session in the same process does not re-announce a platform
    # fact that hasn't changed.
    _unavailable_announced: bool = field(default=False, repr=False, compare=False)

    def read(self) -> "int | None":
        """The current footprint, or ``None`` if this platform (or this
        specific read) produced nothing. Never raises — a reader that
        cannot measure is a disclosed fact (``metric is None`` / a
        transient read failure), not a caller-visible exception."""
        return self.reader()

    def claim_unavailable_announcement(self) -> bool:
        """True the FIRST time this is called on this guard instance,
        False every time after — the guard (not its caller) owns the
        "announced once per process" bookkeeping, since it IS the
        process-wide instance (see the class docstring). A caller that
        just got ``read() is None`` calls this to decide whether it may
        emit ``process_footprint_unavailable``."""
        if self._unavailable_announced:
            return False
        self._unavailable_announced = True
        return True
