"""Command line: colortune [gui|detect|apply|set|reset|daemon|profile]."""
import argparse
import json
import sys

from . import __version__, config


def _displays(probe_ddc=True):
    from .displays import discover
    return discover(probe_ddc=probe_ddc, state=config.load_state())


def cmd_detect(a):
    env, displays = _displays()
    print(f"Session: {env.session}   Desktop: {env.desktop}")
    print(f"Picture method: {env.lut_method or 'none available'}   ddcutil: {'yes' if env.ddc else 'no'}")
    for d in displays:
        print(f"\n{d.name}  [{d.connector}]  key={d.key}")
        g = d.gamut()
        if g:
            print(f"  Gamut (as the display reports it, not measured): {g[0]:.0f}% of sRGB, "
                  f"{d.gamut_p3():.0f}% of Display P3")
        problem = d.edid_problem()
        if problem and d.edid.get("primaries"):
            print(f"  Warning: the display's colour data looks wrong: {problem}")
        for label, ok, detail in d.methods():
            print(f"  {'✓' if ok else '✗'} {label}: {detail}")
        for c in d.controls():
            print(f"    {c.group:8} {c.key:11} {c.label:18} {c.value:g}  [{c.minimum:g}..{c.maximum:g}]")


def _select(displays, which):
    if not which:
        return displays
    sel = [d for d in displays if which in (d.connector, d.key)]
    if not sel:
        sys.exit(f"no display matches {which!r}; see `colortune detect`")
    return sel


def _save(displays):
    def update(state):
        for d in displays:
            state["displays"][d.key] = d.to_state()
    config.update_state(update)


def cmd_set(a):
    values = []
    for kv in a.values:
        k, eq, v = kv.partition("=")
        try:
            if not eq:
                raise ValueError
            values.append((k, float(v)))
        except ValueError:
            sys.exit(f"colortune set: expected control=number, got {kv!r}")
    env, displays = _displays()
    sel = _select(displays, a.display)
    for d in sel:  # check everything before changing anything
        controls = {c.key: c for c in d.controls()}
        for k, _ in values:
            if k not in controls:
                sys.exit(f"{d.connector}: unknown control {k!r}. Available: {', '.join(controls) or 'none'}")
    for d in sel:
        controls = {c.key: c for c in d.controls()}
        for k, v in values:
            c = controls[k]
            c.setter(max(c.minimum, min(c.maximum, v)))
    _finish(displays, a)


def cmd_apply(a):
    from .displays import apply_desktop_saturation
    env, displays = _displays()
    sel = _select(displays, a.display)
    if a.profile:
        prof = config.load_profiles().get(a.profile)
        if prof is None:
            sys.exit(f"no profile named {a.profile!r}")
        for d in sel:
            d.load(prof.get(d.key), from_profile=True)
    hold = a.hold and env.lut_method == "wlr" and any(d.picture_changed() for d in displays)
    for d in sel:
        # At login (no profile) the backlight is left where the user/desktop put it.
        d.apply_all(picture=not hold, backlight=bool(a.profile), force=bool(a.profile))
    apply_desktop_saturation(sel)
    _finish(displays, a)
    if hold:  # become the gamma-holding daemon
        from .backends import wlr
        wlr.send({"cmd": "quit"})
        wlr.serve({d.connector: d.picture for d in displays})


def cmd_reset(a):
    env, displays = _displays(probe_ddc=False)
    for d in _select(displays, a.display):
        d.reset()
    _finish(displays, a)


def _finish(displays, a):
    from .backends import WORKER
    WORKER.flush()
    _save(displays)
    for d in displays:
        if d.last_error:
            print(f"{d.connector}: {d.last_error}", file=sys.stderr)


def cmd_daemon(a):
    from .backends import wlr
    from .displays import discover
    env, displays = discover(probe_ddc=False, state=config.load_state())
    wlr.serve({d.connector: d.picture for d in displays})


def cmd_profile(a):
    if a.action != "list" and not a.name:
        sys.exit(f"colortune profile {a.action}: a profile name is required")
    profiles = config.load_profiles()
    if a.action == "list":
        for name in sorted(profiles):
            print(name)
    elif a.action == "save":
        env, displays = _displays(probe_ddc=False)
        _save(displays)  # live values (backlight, Ada saturation), not a stale state file
        profiles[a.name] = config.load_state()["displays"]
        config.save_profiles(profiles)
    elif a.action == "delete":
        profiles.pop(a.name, None)
        config.save_profiles(profiles)
    elif a.action == "show":
        print(json.dumps(profiles.get(a.name), indent=2))


def cmd_gui(a):
    from .gui.app import main
    return main()


def main(argv=None):
    p = argparse.ArgumentParser(prog="colortune", description="Display calibration for any Linux desktop")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="open the app (default)")
    sub.add_parser("detect", help="show displays and what can be adjusted")
    s = sub.add_parser("set", help="set controls, e.g. `set black=-0.02 gamma=1.05`")
    s.add_argument("-d", "--display", help="connector (eDP-1) or key; default all")
    s.add_argument("values", nargs="+", metavar="control=value")
    s = sub.add_parser("apply", help="re-apply saved settings (use at login)")
    s.add_argument("-d", "--display")
    s.add_argument("-p", "--profile", help="apply a saved profile instead of the last state")
    s.add_argument("--hold", action="store_true", help="on wlroots, stay running to keep gamma applied")
    s = sub.add_parser("reset", help="reset picture curves to neutral")
    s.add_argument("-d", "--display")
    sub.add_parser("daemon", help="hold gamma on wlroots compositors")
    s = sub.add_parser("profile", help="manage named profiles")
    s.add_argument("action", choices=["list", "save", "delete", "show"])
    s.add_argument("name", nargs="?")
    a = p.parse_args(argv)
    fn = {"detect": cmd_detect, "set": cmd_set, "apply": cmd_apply, "reset": cmd_reset,
          "daemon": cmd_daemon, "profile": cmd_profile}.get(a.cmd, cmd_gui)
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
