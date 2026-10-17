"""Persistent state: current settings per display, and named profiles."""
import json
import os
import tempfile
import threading

_STATE_LOCK = threading.RLock()

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "colortune")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
PROFILES_FILE = os.path.join(CONFIG_DIR, "profiles.json")
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
DAEMON_SOCKET = os.path.join(RUNTIME_DIR, "colortune.sock")
AUTOSTART_FILE = os.path.join(os.path.dirname(CONFIG_DIR), "autostart", "colortune-apply.desktop")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True)  # fails before touching any file
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def load_state():
    """{"displays": {key: {"picture": {...}, "hardware": {code: value}}}}"""
    s = _load(STATE_FILE, {})
    s.setdefault("displays", {})
    return s


def save_state(state):
    with _STATE_LOCK:
        _save(STATE_FILE, state)


def update_state(fn):
    """Read-modify-write the state file atomically with respect to this process."""
    with _STATE_LOCK:
        state = load_state()
        fn(state)
        _save(STATE_FILE, state)
        return state


def load_profiles():
    return _load(PROFILES_FILE, {})


def save_profiles(profiles):
    _save(PROFILES_FILE, profiles)


def autostart_enabled():
    return os.path.exists(AUTOSTART_FILE)


def set_autostart(enabled, command):
    if not enabled:
        if os.path.exists(AUTOSTART_FILE):
            os.remove(AUTOSTART_FILE)
        return
    os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
    with open(AUTOSTART_FILE, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=ColorTune (apply settings)\n"
                f"Exec={command} apply --hold\nNoDisplay=true\nX-GNOME-Autostart-Delay=3\n")
