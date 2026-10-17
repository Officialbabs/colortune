"""Backends that can change what a display shows.

hardware : backlight (sysfs/logind), DDC/CI (ddcutil) - changes the monitor itself
lut      : x11 (XRandR gamma), wlr (wlr-gamma-control), icc (colord / KWin vcgt)
matrix   : x11 CTM property, NVIDIA digital vibrance
"""
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Control:
    key: str
    label: str
    group: str                # "hardware" or "picture"
    minimum: float
    maximum: float
    value: float
    setter: Callable[[float], None]
    step: float = 1
    digits: int = 0
    tip: str = ""
    source: str = ""
    default: float = None
    steps: tuple = field(default_factory=tuple)  # calibration steps this control belongs to


def which(cmd):
    return shutil.which(cmd)


def run(args, timeout=10):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


class Coalescer:
    """Runs slow setters on a worker thread, keeping only the latest value per key."""

    def __init__(self):
        self._pending = {}
        self._lock = threading.Lock()
        self._wake = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, key, fn, value):
        with self._lock:
            self._pending[key] = (fn, value)
        self._wake.put(None)

    def _loop(self):
        while True:
            self._wake.get()
            while True:
                with self._lock:
                    if not self._pending:
                        break
                    key = next(iter(self._pending))
                    fn, value = self._pending.pop(key)
                try:
                    fn(value)
                except Exception as e:  # keep the worker alive
                    print(f"colortune: {key}: {e}")

    def flush(self, timeout=10):
        import time
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if not self._pending and self._wake.empty():
                    break
            time.sleep(0.05)


WORKER = Coalescer()
