"""wlroots-family Wayland compositors (Sway, Hyprland, river, labwc, Wayfire, niri...).

Gamma ramps are set through zwlr_gamma_control_manager_v1. The compositor resets them
when the client disconnects, so a small daemon holds the connection; the GUI and CLI
talk to it over a unix socket. Pure-python wire protocol, no extra dependencies.
"""
import json
import os
import selectors
import socket
import struct

from ..config import DAEMON_SOCKET
from ..lut import Picture, ramps_u16

GAMMA_IFACE = "zwlr_gamma_control_manager_v1"


def _str(s):
    b = s.encode() + b"\0"
    return struct.pack("<I", len(b)) + b + b"\0" * ((-len(b)) % 4)


def _read_str(body, off):
    n = struct.unpack_from("<I", body, off)[0]
    s = body[off + 4:off + 4 + n - 1].decode(errors="replace")
    return s, off + 4 + ((n + 3) & ~3)


class Wayland:
    def __init__(self):
        name = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        path = name if name.startswith("/") else os.path.join(os.environ["XDG_RUNTIME_DIR"], name)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.next_id = 2
        self.handlers = {}       # object id -> callable(op, body)
        self.globals = {}        # name -> (iface, version)
        self.buf = b""
        self.registry = self.new_id(self._on_registry)
        self.send(1, 1, struct.pack("<I", self.registry))
        self.roundtrip()

    def new_id(self, handler):
        oid = self.next_id
        self.next_id += 1
        self.handlers[oid] = handler
        return oid

    def send(self, obj, op, payload=b"", fds=()):
        msg = struct.pack("<IHH", obj, op, 8 + len(payload)) + payload
        if fds:
            socket.send_fds(self.sock, [msg], list(fds))
        else:
            self.sock.sendall(msg)

    def bind(self, gname, iface, version, handler):
        oid = self.new_id(handler)
        self.send(self.registry, 0, struct.pack("<I", gname) + _str(iface) + struct.pack("<II", version, oid))
        return oid

    def _on_registry(self, op, body):
        if op == 0:
            gname = struct.unpack_from("<I", body)[0]
            iface, off = _read_str(body, 4)
            self.globals[gname] = (iface, struct.unpack_from("<I", body, off)[0])
            self.on_global(gname, iface)
        elif op == 1:
            gname = struct.unpack_from("<I", body)[0]
            iface = self.globals.pop(gname, ("", 0))[0]
            self.on_global_remove(gname, iface)

    def on_global(self, gname, iface):
        pass

    def on_global_remove(self, gname, iface):
        pass

    def roundtrip(self):
        done = []
        cb = self.new_id(lambda op, body: done.append(1))
        self.send(1, 0, struct.pack("<I", cb))
        while not done:
            self.dispatch(block=True)

    def dispatch(self, block=False):
        self.sock.setblocking(block)
        try:
            data = self.sock.recv(65536)
        except BlockingIOError:
            data = None
        finally:
            self.sock.setblocking(True)
        if data == b"":
            raise ConnectionError("compositor closed the connection")
        if data:
            self.buf += data
        while len(self.buf) >= 8:
            obj, op, size = struct.unpack_from("<IHH", self.buf)
            if len(self.buf) < size:
                break
            body, self.buf = self.buf[8:size], self.buf[size:]
            if obj == 1 and op == 0:
                oid, code = struct.unpack_from("<II", body)
                raise RuntimeError(f"wayland error on object {oid} code {code}: {_read_str(body, 8)[0]}")
            h = self.handlers.get(obj)
            if h:
                h(op, body)


class GammaClient(Wayland):
    """Tracks outputs by name and holds a gamma control for each."""

    def __init__(self):
        self.outputs = {}        # global name -> dict(id, name, gamma, size, failed)
        self.last = {}           # output name -> Picture, re-applied when a monitor is replugged
        self.manager = None
        self.xdg_manager = None
        super().__init__()
        for gname, (iface, ver) in list(self.globals.items()):
            if iface == GAMMA_IFACE:
                self.manager = self.bind(gname, iface, 1, lambda op, b: None)
            elif iface == "zxdg_output_manager_v1":
                self.xdg_manager = self.bind(gname, iface, min(ver, 3), lambda op, b: None)
        for gname, (iface, ver) in list(self.globals.items()):
            if iface == "wl_output":
                self._add_output(gname, ver)
        self.roundtrip()
        self.roundtrip()

    @property
    def supported(self):
        return self.manager is not None

    def on_global(self, gname, iface):
        if iface == "wl_output" and self.manager is not None:  # hotplug after start
            self._add_output(gname, self.globals[gname][1])

    def on_global_remove(self, gname, iface):
        self.outputs.pop(gname, None)

    def _add_output(self, gname, ver):
        o = {"name": None, "gamma": None, "size": 0, "failed": False, "pending": None}
        self.outputs[gname] = o

        def named(name):
            o["name"] = name
            pic = self.last.get(name)
            if pic is not None and o["pending"] is None and self.manager:
                self.set_picture(name, pic)

        def on_output(op, body):
            if op == 4:  # wl_output.name (v4)
                named(_read_str(body, 0)[0])
        o["id"] = self.bind(gname, "wl_output", min(ver, 4), on_output)
        if ver < 4 and self.xdg_manager:
            def on_xdg(op, body):
                if op == 3:
                    named(_read_str(body, 0)[0])
            xid = self.new_id(on_xdg)
            self.send(self.xdg_manager, 1, struct.pack("<II", xid, o["id"]))

    def _get_gamma(self, o):
        def on_gamma(op, body):
            if op == 0:
                o["size"] = struct.unpack_from("<I", body)[0]
                if o["pending"]:
                    self.set_picture(o["name"], o["pending"])
            elif op == 1:
                o["failed"] = True
        o["gamma"] = self.new_id(on_gamma)
        self.send(self.manager, 0, struct.pack("<II", o["gamma"], o["id"]))

    def by_name(self, name):
        return next((o for o in self.outputs.values() if o["name"] == name), None)

    def set_picture(self, output, picture: Picture):
        self.last[output] = picture
        o = self.by_name(output)
        if not o:
            return f"unknown output {output}"
        o["pending"] = picture
        if picture.lut_identity():
            # Neutral: let go of the output so the compositor restores its own ramps
            # and other tools (gammastep, wlsunset...) can use gamma control again.
            if o["gamma"] is not None:
                self.send(o["gamma"], 1)
                self.handlers.pop(o["gamma"], None)
            o.update(gamma=None, size=0, failed=False)
            return None
        if o["gamma"] is None and self.manager:
            o["failed"] = False
            self._get_gamma(o)
            return None  # applied once gamma_size arrives
        if o["failed"]:
            return "compositor refused gamma control (another app such as gammastep/wlsunset may own it)"
        if not o["size"]:
            return None  # applied once gamma_size arrives
        data = b"".join(struct.pack("=%dH" % o["size"], *ch) for ch in ramps_u16(picture, o["size"]))
        fd = os.memfd_create("colortune-gamma", os.MFD_CLOEXEC)
        try:
            os.write(fd, data)
            os.lseek(fd, 0, os.SEEK_SET)
            self.send(o["gamma"], 0, b"", fds=[fd])
        finally:
            os.close(fd)
        return None


def probe():
    """True if the running compositor offers wlr gamma control."""
    if not os.environ.get("WAYLAND_DISPLAY"):
        return False
    try:
        w = Wayland()
        ok = any(i == GAMMA_IFACE for i, _ in w.globals.values())
        w.sock.close()
        return ok
    except Exception:
        return False


# ---------------------------------------------------------------- daemon side

def serve(initial):
    """Run the gamma-holding daemon. initial: {output_name: Picture}."""
    client = GammaClient()
    if not client.supported:
        raise SystemExit("compositor does not support wlr-gamma-control")
    client.roundtrip()
    for name, pic in initial.items():
        client.set_picture(name, pic)
    client.roundtrip()

    if os.path.exists(DAEMON_SOCKET):
        os.unlink(DAEMON_SOCKET)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(DAEMON_SOCKET)
    srv.listen(4)
    sel = selectors.DefaultSelector()
    sel.register(srv, selectors.EVENT_READ, "srv")
    sel.register(client.sock, selectors.EVENT_READ, "wl")
    print(f"colortune daemon: holding gamma for {[o['name'] for o in client.outputs.values()]}")
    try:
        while True:
            for key, _ in sel.select():
                if key.data == "wl":
                    client.dispatch()
                elif key.data == "srv":
                    conn, _ = srv.accept()
                    sel.register(conn, selectors.EVENT_READ, "conn")
                else:
                    conn = key.fileobj
                    line = b""
                    while not line.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        line += chunk
                    if not line:
                        sel.unregister(conn)
                        conn.close()
                        continue
                    try:
                        msg = json.loads(line)
                        if msg.get("cmd") == "ping":
                            conn.sendall(b'{"ok": true}\n')
                            continue
                        if msg.get("cmd") == "quit":
                            conn.sendall(b'{"ok": true}\n')
                            return
                        err = client.set_picture(msg["output"], Picture.from_dict(msg["picture"]))
                        reply = {"ok": err is None, "error": err}
                    except Exception as e:
                        reply = {"ok": False, "error": str(e)}
                    conn.sendall(json.dumps(reply).encode() + b"\n")
    finally:
        sel.close()
        srv.close()
        if os.path.exists(DAEMON_SOCKET):
            os.unlink(DAEMON_SOCKET)


def send(msg, timeout=3):
    """Send one request to the daemon. Returns reply dict, or None if it isn't running."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(DAEMON_SOCKET)
        s.sendall(json.dumps(msg).encode() + b"\n")
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            reply += chunk
        s.close()
        return json.loads(reply) if reply else None
    except (OSError, ValueError):
        return None
