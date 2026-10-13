"""Being a good neighbour on a shared machine."""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NICE_INCREMENT = 10


def lower_process_priority() -> bool:
    """Drop this process below normal priority so everything else on the machine wins the CPU when it
    wants it. Children started afterwards (ffmpeg, yt-dlp) inherit it on both Windows and POSIX, and so
    do the threads Whisper and the face detector spin up. Returns whether it worked."""
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS):
                raise OSError(ctypes.get_last_error(), "SetPriorityClass failed")
        else:
            os.nice(NICE_INCREMENT)
        return True
    except (OSError, AttributeError) as exc:
        log.warning("Could not lower process priority: %s", exc)
        return False
