"""Colour boost: saturation from the graphics chip's colour matrix (colortune/kms.py).

The app reads the state as the user. Changing it needs root, so it goes through pkexec to
the helper: the installed copy, whose polkit action remembers the password for a few
minutes, or this checkout's kms.py for the one-time set-up (and updates).
"""
import filecmp
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from .. import kms


@dataclass
class State:
    now: Optional[float]      # on screen now; None when another program set a matrix
    seen: bool                # whether the screen could be read at all
    installed: bool
    outdated: bool            # the installed helper differs from this version
    startup: Optional[float]  # set at every startup, or None
    startup_possible: bool    # only with greetd


def available(env):
    """Offered on Ada, which leaves the colour matrix alone (unlike compositors that manage
    colour themselves), when pkexec and a card with a colour matrix are there."""
    return bool(env.ada and env.session == "wayland" and shutil.which("pkexec") and kms.has_ctm())


def _helper():
    """(installed, outdated): outdated when the installed copy differs from this version."""
    installed = os.path.exists(kms.LIBEXEC) and os.path.exists(kms.POLICY)
    return installed, installed and not filecmp.cmp(kms.LIBEXEC, kms.__file__, shallow=False)


def state(prev=None):
    """Read as the user. When the screen can't be read, the previous value stands."""
    s = kms.summary()
    installed, outdated = _helper()
    now = s["now"] if s["seen"] or prev is None else prev.now
    return State(now, s["seen"], installed, outdated, s["startup"],
                 (s["display_manager"] or "").startswith("greetd"))


def _python():
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


def run(*args):
    """Run the helper as root through pkexec (which asks for the password, for as long as
    the person takes). Returns None, "cancelled", or what went wrong."""
    installed, outdated = _helper()
    if installed and not outdated and args[0] != "install":
        helper = [kms.LIBEXEC]
    else:
        helper = [_python(), "-I", os.path.realpath(kms.__file__)]
    try:
        p = subprocess.run(["pkexec", *helper, *args], capture_output=True, text=True)
    except OSError as e:
        return str(e)
    if p.returncode == 0:
        return None
    err = [line.strip() for line in p.stderr.splitlines() if line.strip()]
    if p.returncode == 126:
        return "cancelled"  # the password dialog was dismissed
    if not any(line.startswith("colortune-ctm:") for line in err):
        low = p.stderr.lower()
        if "authentication agent" in low:
            return "no password dialog is available (Ada's polkit agent isn't running)"
        if "not authorized" in low:
            return "this account isn't allowed to make system changes"
        if "Traceback" in p.stderr:
            return f"the helper stopped with an error: {err[-1]}"
    text = " ".join(line.removeprefix("colortune-ctm: ") for line in err)
    return text or f"the helper failed ({p.returncode})"
