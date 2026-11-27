"""Giving memory that is no longer used back to the system (Linux only).

On Linux the C library keeps a separate pool of memory for each thread that asks
for some, and GStreamer and Qt run many threads. Memory freed in those pools,
such as a song's cover and decoded sound after the next song starts, stays with
the program instead of going back to the system, so its memory use only grows.
Fewer pools, and trimming them after each song, keep it down.

Elsewhere (Windows) these do nothing.
"""

import ctypes
import sys

_M_ARENA_MAX = -8  # from glibc's malloc.h
_MAX_POOLS = 2
_TRIM_DELAY_MS = 3000  # after a song starts, once the previous one's memory is freed


def _glibc():
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL(None)  # the C library the program already uses
        libc.mallopt  # glibc has these; another C library (musl) may not
        libc.malloc_trim
    except (OSError, AttributeError):
        return None
    return libc


_libc = _glibc()
_timer = None


def limit_pools() -> None:
    """Use few memory pools. Call before Qt and GStreamer start their threads."""
    if _libc is not None:
        _libc.mallopt(_M_ARENA_MAX, _MAX_POOLS)


def trim_soon() -> None:
    """Give freed memory back to the system in a moment (a song just started)."""
    global _timer
    if _libc is None:
        return
    if _timer is None:
        from PySide6.QtCore import QTimer

        _timer = QTimer()
        _timer.setSingleShot(True)
        _timer.setInterval(_TRIM_DELAY_MS)
        _timer.timeout.connect(lambda: _libc.malloc_trim(0))
    _timer.start()  # a new song before it fired: wait for that one instead
