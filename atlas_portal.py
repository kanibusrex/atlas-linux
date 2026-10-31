#!/usr/bin/env python3
"""
Atlas as the file picker other apps use: an xdg-desktop-portal FileChooser backend.

When an app asks to open or save a file through the portal (browsers, GTK and Qt apps set to use it,
Flatpaks), xdg-desktop-portal calls this service, which opens an Atlas window in picking mode
(atlas.py --pick) and passes back what was chosen. Each request is its own Atlas window, so a picker
never disturbs the Atlas you have open.

Run by D-Bus on demand (the AppImage with --portal). Needs, in ~/.local/share:
  xdg-desktop-portal/portals/atlas.portal    names this service and the interface it serves
  dbus-1/services/org.freedesktop.impl.portal.desktop.atlas.service
and FileChooser=atlas in ~/.config/xdg-desktop-portal/<desktop>-portals.conf. install.sh --picker does all three.
"""

import fnmatch
import json
import os
import sys

from gi.repository import Gio, GLib

HERE = os.path.dirname(os.path.abspath(__file__))
BUS_NAME = "org.freedesktop.impl.portal.desktop.atlas"
OBJ_PATH = "/org/freedesktop/portal/desktop"

XML = """<node>
<interface name="org.freedesktop.impl.portal.FileChooser">
  <method name="OpenFile">
    <arg type="o" name="handle" direction="in"/><arg type="s" name="app_id" direction="in"/>
    <arg type="s" name="parent_window" direction="in"/><arg type="s" name="title" direction="in"/>
    <arg type="a{sv}" name="options" direction="in"/>
    <arg type="u" name="response" direction="out"/><arg type="a{sv}" name="results" direction="out"/>
  </method>
  <method name="SaveFile">
    <arg type="o" name="handle" direction="in"/><arg type="s" name="app_id" direction="in"/>
    <arg type="s" name="parent_window" direction="in"/><arg type="s" name="title" direction="in"/>
    <arg type="a{sv}" name="options" direction="in"/>
    <arg type="u" name="response" direction="out"/><arg type="a{sv}" name="results" direction="out"/>
  </method>
  <method name="SaveFiles">
    <arg type="o" name="handle" direction="in"/><arg type="s" name="app_id" direction="in"/>
    <arg type="s" name="parent_window" direction="in"/><arg type="s" name="title" direction="in"/>
    <arg type="a{sv}" name="options" direction="in"/>
    <arg type="u" name="response" direction="out"/><arg type="a{sv}" name="results" direction="out"/>
  </method>
</interface>
<interface name="org.freedesktop.impl.portal.Request">
  <method name="Close"/>
</interface>
</node>"""

OK, CANCELLED, FAILED = 0, 1, 2


def log(*a):
    print("atlas-portal:", *a, file=sys.stderr, flush=True)


# ---- Filters: the portal gives globs and MIME types; the page only matches names, so types become globs
_globs = None


def mime_globs():
    """{mime type: [globs]} from the shared MIME database."""
    global _globs
    if _globs is None:
        _globs = {}
        dirs = [GLib.get_user_data_dir()] + list(GLib.get_system_data_dirs())
        for d in reversed(dirs):
            try:
                with open(os.path.join(d, "mime", "globs2"), encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("#"):
                            continue
                        parts = line.rstrip("\n").split(":")
                        if len(parts) >= 3:
                            _globs.setdefault(parts[1], []).append(parts[2])
            except OSError:
                pass
        # aliases and subclasses are matched too (text/plain takes in source code, and so on)
        for d in reversed(dirs):
            for name in ("aliases", "subclasses"):
                try:
                    with open(os.path.join(d, "mime", name), encoding="utf-8") as f:
                        for line in f:
                            a, _, b = line.strip().partition(" ")
                            if name == "aliases" and b in _globs:
                                _globs.setdefault(a, []).extend(_globs[b])
                            elif name == "subclasses" and a in _globs:
                                _globs.setdefault(b, []).extend(_globs[a])
                except OSError:
                    pass
    return _globs


def globs_for(rules):
    """A portal filter's rules [(0, glob) | (1, mime type)] as globs; None means anything goes."""
    out = []
    for kind, pat in rules:
        if kind == 0:
            if pat in ("*", "*.*"):
                return None
            out.append(pat)
        elif pat in ("*", "*/*", "application/octet-stream"):
            return None
        elif pat.endswith("/*"):
            pre = pat[:-1]
            for m, gs in mime_globs().items():
                if m.startswith(pre):
                    out.extend(gs)
        else:
            out.extend(mime_globs().get(pat, []))
    return sorted(set(out))


def path_of(v):
    """A portal 'ay' path (NUL-terminated bytes) as text."""
    if v is None:
        return ""
    b = bytes(v) if not isinstance(v, (bytes, bytearray)) else v
    return b.rstrip(b"\0").decode("utf-8", "surrogateescape")


class Portal:
    def __init__(self):
        self.loop = GLib.MainLoop()
        self.running = {}   # handle -> Gio.Subprocess
        self.regs = {}      # handle -> registration id of its Request object
        self.conn = None
        self.idle = 0
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        self.chooser_info, self.request_info = node.interfaces
        Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, self.on_bus, None, lambda *_a: self.loop.quit())

    def on_bus(self, conn, _name):
        self.conn = conn
        conn.register_object(OBJ_PATH, self.chooser_info, self.on_call, None, None)
        log("ready")
        self.idle_later()

    # D-Bus starts the service again when it is needed, so it leaves after a quiet while
    def idle_later(self):
        if self.idle:
            GLib.source_remove(self.idle)
        self.idle = GLib.timeout_add_seconds(600, self.on_idle)

    def on_idle(self):
        self.idle = 0
        if not self.running:
            self.loop.quit()
            return False
        self.idle_later()
        return False

    def on_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        handle, app_id, _parent, title, options = params.unpack()
        try:
            pick, filters = self.request(method, title, options)
        except Exception as e:  # noqa: BLE001 - whatever it is, the app gets an answer
            log("bad request:", e)
            return invocation.return_value(GLib.Variant("(ua{sv})", (FAILED, {})))
        log(method, "for", app_id or "an app", "-", pick["mode"], pick.get("folder") or "")
        self.idle_later()
        self.spawn(handle, method, pick, filters, options, invocation)

    def request(self, method, title, options):
        """What the Atlas window needs to know, from the portal's request."""
        pick = {"mode": {"OpenFile": "open", "SaveFile": "save", "SaveFiles": "savefiles"}[method], "title": title or ""}
        if "accept_label" in options:
            pick["accept"] = options["accept_label"].replace("_", "")
        pick["multiple"] = bool(options.get("multiple", False))
        pick["directory"] = bool(options.get("directory", False))
        filters = list(options.get("filters") or [])
        pick["filters"] = [[name, globs_for(rules)] for name, rules in filters]
        cur = options.get("current_filter")
        if cur:
            if cur in filters:
                pick["current"] = filters.index(cur)
            else:   # an app may name a filter it did not list: it goes first and is the one shown
                filters.insert(0, cur)
                pick["filters"].insert(0, [cur[0], globs_for(cur[1])])
                pick["current"] = 0
        folder = path_of(options.get("current_folder"))
        current_file = path_of(options.get("current_file"))
        if current_file:
            folder = folder or os.path.dirname(current_file)
            pick["name"] = os.path.basename(current_file)
        if options.get("current_name"):
            pick["name"] = options["current_name"]
        if folder and os.path.isdir(folder):
            pick["folder"] = folder
        if method == "SaveFiles":
            pick["files"] = [os.path.basename(path_of(f)) for f in options.get("files") or []]
        return pick, filters

    def spawn(self, handle, method, pick, filters, options, invocation):
        try:
            proc = Gio.Subprocess.new([sys.executable, "-I", os.path.join(HERE, "atlas.py"), "--pick", json.dumps(pick)],
                                      Gio.SubprocessFlags.STDOUT_PIPE)
        except GLib.Error as e:
            log("could not start Atlas:", e.message)
            return invocation.return_value(GLib.Variant("(ua{sv})", (FAILED, {})))
        self.running[handle] = proc
        try:
            self.regs[handle] = self.conn.register_object(handle, self.request_info, self.on_close, None, None)
        except GLib.Error:
            pass

        def done(p, res):
            self.running.pop(handle, None)
            rid = self.regs.pop(handle, 0)
            if rid:
                self.conn.unregister_object(rid)
            try:
                _ok, out, _err = p.communicate_utf8_finish(res)
                ans = json.loads(out) if out and out.strip() else None
            except (GLib.Error, ValueError) as e:
                log("Atlas picker failed:", e)
                return invocation.return_value(GLib.Variant("(ua{sv})", (FAILED, {})))
            if not ans or not ans.get("paths"):
                return invocation.return_value(GLib.Variant("(ua{sv})", (CANCELLED, {})))
            paths = ans["paths"]
            if method == "SaveFiles":   # a folder was chosen; the files go in it, under their own names
                paths = [os.path.join(paths[0], n) for n in pick.get("files") or []]
            # always file:// URIs, which is all the portal takes: Gio would give smb://... for a file on a network share
            res = {"uris": GLib.Variant("as", [GLib.filename_to_uri(os.path.abspath(p), None) for p in paths])}
            fi = ans.get("filter")
            if isinstance(fi, int) and 0 <= fi < len(filters):
                res["current_filter"] = GLib.Variant("(sa(us))", filters[fi])
            if options.get("choices"):   # extra options the app offered are left as they were
                res["choices"] = GLib.Variant("a(ss)", [(c[0], c[3]) for c in options["choices"]])
            invocation.return_value(GLib.Variant("(ua{sv})", (OK, res)))

        proc.communicate_utf8_async(None, None, done)

    def on_close(self, _conn, _sender, path, _iface, _method, _params, invocation):
        proc = self.running.get(path)
        if proc:
            proc.force_exit()
        invocation.return_value(None)

    def run(self):
        self.loop.run()


if __name__ == "__main__":
    Portal().run()
