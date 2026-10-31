#!/usr/bin/env python3
"""
Atlas: a file explorer, in a WebKitGTK window. The Linux counterpart of AtlasShell.ps1.

The window here does the file work; atlas.html, beside this script, is what you see. The page asks the
window for things by message (list a folder, search, copy...), exactly as it does in WebView2 on
Windows: a small script gives the page the same window.chrome.webview it expects. Anything that can
take a while runs in a background thread, so the window never freezes, and its progress is passed back
to the page as it goes. atlas_core.py holds the file operations.

Run:   python3 atlas.py [folder]
       python3 atlas.py --pick '{"mode": "open"}'   a window for choosing files, for another app (see atlas_portal.py)
Its settings and browser profile are kept in ~/.local/share/atlas.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, WebKit2  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import atlas_core as core  # noqa: E402

APP_ID = "dev.atlas.Atlas"
VERSION = "1.1.1"
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "atlas")
PAGE = os.path.join(HERE, "atlas.html")
BG = "#1a1b1e"
PICK_STATE = os.path.join(DATA_DIR, "pick.json")   # the folder last picked from, for the next picker

# what the page is given before its own script runs: the window.chrome.webview it talks to on Windows
BRIDGE = r"""
window.__atlasLinux = true;
(function () {
  var listeners = [];
  window.chrome = window.chrome || {};
  window.chrome.webview = {
    postMessage: function (m) { window.webkit.messageHandlers.atlas.postMessage(JSON.stringify(m)); },
    addEventListener: function (type, fn) { if (type === "message") { listeners.push(fn); } },
    removeEventListener: function (type, fn) { listeners = listeners.filter(function (f) { return f !== fn; }); }
  };
  window.__atlasRecv = function (d) { listeners.forEach(function (fn) { try { fn({ data: d }); } catch (e) { setTimeout(function () { throw e; }); } }); };
})();
"""

TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".bmp": "image/bmp", ".ico": "image/x-icon", ".svg": "image/svg+xml", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/mp4", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".flac": "audio/flac",
}


# Omarchy's current theme: its palette, resolved by Omarchy's own omarchy-theme-color (the same
# colours its terminals, bar and browser get), and the folder that changes when the theme does
OMARCHY_STATE = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "omarchy", "current")


def omarchy_palette():
    exe = shutil.which("omarchy-theme-color") or os.path.join(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy"), "bin", "omarchy-theme-color")
    if not os.path.isfile(exe):
        return None
    try:
        r = subprocess.run([exe, "--all"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    pal = {}
    for ln in r.stdout.splitlines():
        k, _, v = ln.partition("\t")
        if k and v:
            pal[k.strip()] = v.strip()
    if not pal.get("background") or not pal.get("foreground"):
        return None
    try:
        with open(os.path.join(OMARCHY_STATE, "theme.name")) as f:
            pal["name"] = f.read().strip()
    except OSError:
        pal["name"] = ""
    pal["font"] = omarchy_font()
    return pal


def omarchy_font():
    """Omarchy's font (what omarchy-font-set chose): the first family fontconfig gives for monospace."""
    try:
        r = subprocess.run(["fc-match", "monospace", "-f", "%{family}"], capture_output=True, text=True, timeout=5)
        return r.stdout.split(",")[0].strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def to_json(v):
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False, default=str)


class Job:
    def __init__(self, jid, op, args):
        self.id, self.op, self.args = jid, op, args
        self.state = core.State()
        self.done = False
        self.result = None
        self.error = ""
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.result = core.run(self.op, self.args, self.state)
        except Exception as e:  # anything a request hits is told to the page, never lost
            self.error = getattr(e, "strerror", None) or str(e) or e.__class__.__name__
        self.done = True


class Copy:
    def __init__(self, cid, a):
        srcs = list(a.get("sources") or [])
        self.id = cid
        self.args = {"sources": srcs, "dest": a.get("dest") or "", "move": bool(a.get("move")),
                     "policy": a.get("policy") or "", "dests": a.get("dests")}
        self.status = "waiting"
        self.job = None
        self.state = core.State()
        self.result = None
        self.error = ""
        self.started = self.ended = None
        self.label = os.path.basename(str(srcs[0]).rstrip("/")) if len(srcs) == 1 else "%d items" % len(srcs)

    def as_row(self):
        s = self.state
        elapsed = 0
        if self.started:
            elapsed = int(((self.ended or time.monotonic()) - self.started) * 1000)
        return {"id": self.id, "label": self.label, "dest": self.args["dest"], "move": self.args["move"], "status": self.status,
                "paused": bool(s.paused), "phase": s.phase, "bytesDone": s.bytes_done, "bytesTotal": s.bytes_total,
                "filesDone": s.files_done, "filesTotal": s.files_total, "current": s.current, "elapsed": elapsed,
                "result": self.result, "error": self.error}


class AtlasWindow(Gtk.ApplicationWindow):
    def __init__(self, app, start, pick=None):
        super().__init__(application=app, title="Atlas")
        # choosing files for another app: what it asked for (mode, filters, folder, name...); the answer goes to stdout.
        # It starts in the folder the app gave, or the one last picked from, or home.
        self.pick = pick
        if pick:
            start = pick.get("folder") or ""
            if not os.path.isdir(start):
                try:
                    with open(PICK_STATE) as f:
                        start = json.load(f).get("folder") or ""
                except (OSError, ValueError, AttributeError):
                    start = ""
            if not os.path.isdir(start):
                start = os.path.expanduser("~")
        # folders asked for before the page has loaded wait for it (Atlas started by "Show in folder")
        self.page_ready, self.pending_open = False, []
        # a file to start on is shown in its folder, selected
        self.start_select = ""
        if start and os.path.exists(start) and not os.path.isdir(start):
            start, self.start_select = os.path.dirname(os.path.abspath(start)), os.path.basename(start)
        self.start_path = start
        self.jobs = {}
        self.copies = []
        self.copy_seq = 0
        self.copies_sent = ""
        self.last_copy_send = 0.0
        self.monitors = {}
        self.omarchy_mon, self.omarchy_t = None, 0
        try:
            self.omarchy_mon = Gio.File.new_for_path(OMARCHY_STATE).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.omarchy_mon.connect("changed", self.on_omarchy_changed)
        except GLib.Error:
            pass
        # omarchy-font-set rewrites this file; the new font is passed on like a new theme
        try:
            fc = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "fontconfig")
            self.font_mon = Gio.File.new_for_path(fc).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self.font_mon.connect("changed", self.on_omarchy_changed)
        except GLib.Error:
            self.font_mon = None
        self.changed = set()
        self.changed_sent = {}
        self.icon_cache = {}
        self.questions, self.qseq = {}, 0
        # drives coming and going (a USB stick in or out, a disk mounted): the page redraws its drives
        self.vmon = Gio.VolumeMonitor.get()
        self.vols_t = 0
        for sig in ("volume-added", "volume-removed", "volume-changed", "mount-added", "mount-removed"):
            self.vmon.connect(sig, self.on_volumes)
        self.prefs_path = os.path.join(DATA_DIR, "atlas.json")
        self.prefs = {"W": 1360, "H": 860, "Max": False, "Dark": True, "Zoom": 1.15}
        try:
            with open(self.prefs_path) as f:
                self.prefs.update(json.load(f))
        except (OSError, ValueError):
            pass

        if pick:
            self.set_default_size(min(int(self.prefs["W"]), 1100), min(int(self.prefs["H"]), 700))
            self.set_title(pick.get("title") or ("Save" if pick.get("mode") != "open" else "Open"))
        else:
            self.set_default_size(int(self.prefs["W"]), int(self.prefs["H"]))
        self.set_size_request(760, 480)
        if self.prefs.get("Max") and not pick:
            self.maximize()
        ico = os.path.join(HERE, "atlas.png")
        if os.path.exists(ico):
            try:
                self.set_icon_from_file(ico)
            except GLib.Error:
                pass
        Gtk.Settings.get_default().props.gtk_application_prefer_dark_theme = bool(self.prefs.get("Dark", True))

        # the browser profile (the page's saved favourites, tabs and theme) lives in Atlas's own folder
        os.makedirs(DATA_DIR, exist_ok=True)
        mgr = WebKit2.WebsiteDataManager(base_data_directory=os.path.join(DATA_DIR, "profile"),
                                         base_cache_directory=os.path.join(DATA_DIR, "cache"))
        ctx = WebKit2.WebContext(website_data_manager=mgr)
        ctx.register_uri_scheme("atlas", self.answer_resource)
        sec = ctx.get_security_manager()
        sec.register_uri_scheme_as_secure("atlas")
        sec.register_uri_scheme_as_cors_enabled("atlas")

        ucm = WebKit2.UserContentManager()
        ucm.add_script(WebKit2.UserScript(BRIDGE, WebKit2.UserContentInjectedFrames.TOP_FRAME,
                                          WebKit2.UserScriptInjectionTime.START, None, None))
        ucm.connect("script-message-received::atlas", self.on_message)
        ucm.register_script_message_handler("atlas")

        self.view = WebKit2.WebView(web_context=ctx, user_content_manager=ucm)
        rgba = Gdk.RGBA()
        rgba.parse(BG)
        self.view.set_background_color(rgba)
        # how big everything is drawn: a little bigger than the page's own sizes by default, Ctrl+= and Ctrl+- change it
        self.view.set_zoom_level(float(self.prefs.get("Zoom", 1.15)))
        st = self.view.get_settings()
        st.set_enable_developer_extras(bool(os.environ.get("ATLAS_DEBUG")))
        if os.environ.get("ATLAS_DEBUG") and hasattr(st, "set_enable_write_console_messages_to_stdout"):
            st.set_enable_write_console_messages_to_stdout(True)
        st.set_enable_back_forward_navigation_gestures(False)
        st.set_javascript_can_access_clipboard(True)
        st.set_enable_html5_local_storage(True)
        st.set_enable_html5_database(True)
        self.view.connect("context-menu", lambda *a: not os.environ.get("ATLAS_DEBUG"))
        self.view.connect("decide-policy", self.on_policy)
        self.view.connect("create", lambda *a: None)
        self.add(self.view)

        self.connect("delete-event", self.on_close)
        self.connect("window-state-event", self.on_state)

        q = "?path=" + urllib.parse.quote(os.path.abspath(start), safe="") if start else ""
        if start and self.start_select:
            q += "&select=" + urllib.parse.quote(self.start_select, safe="")
        if pick:
            q += ("&" if q else "?") + "pick=" + urllib.parse.quote(json.dumps(pick), safe="")
        self.view.load_uri("atlas://app/atlas.html" + q)
        GLib.timeout_add(50, self.tick)
        self.was_max = None

    # ---- Talking to the page ----------------------------------------------------------------------
    def send_page(self, obj_json):
        js = "window.__atlasRecv && window.__atlasRecv(" + obj_json + ");"
        if hasattr(self.view, "evaluate_javascript"):
            self.view.evaluate_javascript(js, -1, None, None, None, None, None)
        else:
            self.view.run_javascript(js, None, None, None)

    def reply(self, rid, data=None, error=""):
        if error:
            self.send_page('{"re":%d,"ok":false,"error":%s}' % (rid, to_json(error)))
        else:
            self.send_page('{"re":%d,"ok":true,"data":%s}' % (rid, to_json(data)))

    def on_message(self, _ucm, js_result):
        try:
            v = js_result.get_js_value() if hasattr(js_result, "get_js_value") else js_result
            m = json.loads(v.to_string())
        except Exception:
            return
        rid = int(m.get("id") or 0)
        try:
            self.receive(rid, str(m.get("op") or ""), m)
        except Exception as e:
            self.reply(rid, error=str(e) or e.__class__.__name__)

    def receive(self, rid, op, m):
        if op in core.WINDOWS_ONLY or op in ("list", "drives", "search", "sizes", "children", "rename", "mkdir", "newfile", "delete",
                                             "conflicts", "readtext", "details", "recyclable", "hash", "compare", "zip", "unzip", "zipopen", "smbshares", "restore", "emptytrash",
                                             "perms", "chperm", "inuse", "endprogram", "wallpaper", "moveback"):
            j = Job(rid, op, m)
            self.jobs[rid] = j
            j.thread.start()
            return
        if op == "cancel":
            j = self.jobs.get(int(m.get("job") or 0))
            if j:
                j.state.cancel = True
            return self.reply(rid, True)
        if op == "open":
            for p in m.get("paths") or []:
                Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(p)).get_uri(), None)
            return self.reply(rid, True)
        if op == "openwith":
            return self.open_with(rid, str(m.get("path")))
        if op == "reveal":
            self.reveal(str(m.get("path")))
            return self.reply(rid, True)
        if op == "explorer":
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(m.get("path"))).get_uri(), None)
            return self.reply(rid, True)
        if op == "terminal":
            self.terminal(str(m.get("path")))
            return self.reply(rid, True)
        if op == "smbmount":
            return self.smb_mount(rid, m)
        if op == "mountanswer":
            mo = self.questions.pop(int(m.get("id") or 0), None)
            if mo:
                ch = m.get("choice")
                if ch is None:
                    mo.reply(Gio.MountOperationResult.ABORTED)
                else:
                    mo.set_choice(int(ch))
                    mo.reply(Gio.MountOperationResult.HANDLED)
            return self.reply(rid, True)
        if op == "volumes":
            return self.reply(rid, self.volumes())
        if op == "mountvol":
            return self.mount_volume(rid, str(m.get("id") or ""))
        if op == "eject":
            return self.eject(rid, str(m.get("path") or ""), bool(m.get("force")))
        if op == "smbunmount":
            return self.smb_unmount(rid, str(m.get("path") or ""), bool(m.get("force")))
        if op == "properties":
            return self.reply(rid, error="Use the preview pane for an item's details.")
        if op == "clipset":
            self.clip_set([str(p) for p in m.get("paths") or []], bool(m.get("cut")))
            return self.reply(rid, True)
        if op == "clipget":
            return self.reply(rid, self.clip_get())
        if op == "copytext":
            cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            cb.set_text(str(m.get("text") or ""), -1)
            cb.store()
            return self.reply(rid, True)
        if op == "savetext":
            return self.save_text(rid, str(m.get("name") or "export.csv"), str(m.get("text") or ""))
        if op == "url":
            u = str(m.get("url") or "")
            if u.startswith("https://"):
                Gio.AppInfo.launch_default_for_uri(u, None)
            return self.reply(rid, True)
        if op == "copy":
            self.copy_seq += 1
            self.copies.append(Copy(self.copy_seq, m))
            self.start_next_copy()
            self.send_copies(True)
            return self.reply(rid, {"copy": self.copy_seq})
        if op == "copyctl":
            c = next((x for x in self.copies if x.id == int(m.get("copy") or 0)), None)
            act = str(m.get("action") or "")
            if act == "pause" and c:
                c.state.paused = True
            elif act == "resume" and c:
                c.state.paused = False
            elif act == "cancel" and c:
                c.state.cancel, c.state.paused = True, False
                if c.status == "waiting":
                    c.status = "cancelled"
            elif act == "clear":
                self.copies = [x for x in self.copies if x.status in ("running", "waiting")]
            self.send_copies(True)
            return self.reply(rid, True)
        if op == "watch":
            self.watch([str(p) for p in m.get("paths") or []])
            return self.reply(rid, True)
        if op == "zoom":
            z = max(0.7, min(2.0, float(m.get("level") or 1.15)))
            self.view.set_zoom_level(z)
            self.prefs["Zoom"] = z
            return self.reply(rid, z)
        if op == "omarchy":
            return self.reply(rid, omarchy_palette())
        if op == "theme":
            self.prefs["Dark"] = bool(m.get("dark"))
            Gtk.Settings.get_default().props.gtk_application_prefer_dark_theme = self.prefs["Dark"]
            return self.reply(rid, True)
        if op == "picked":
            # the answer for the app that asked; closing the window without one is a cancel
            if self.pick and not self.pick.get("done"):
                self.pick["done"] = True
                paths = [str(p) for p in m.get("paths") or []]
                sys.stdout.write(json.dumps({"paths": paths, "filter": m.get("filter")}) + "\n")
                sys.stdout.flush()
                if paths:
                    last = paths[0] if self.pick.get("directory") or self.pick.get("mode") == "savefiles" else os.path.dirname(paths[0])
                    try:
                        with open(PICK_STATE, "w") as f:
                            json.dump({"folder": last}, f)
                    except OSError:
                        pass
            self.reply(rid, True)
            return self.close()
        if op == "title":
            if self.pick:
                return self.reply(rid, True)
            t = str(m.get("text") or "")
            self.set_title(t + " - Atlas" if t else "Atlas")
            return self.reply(rid, True)
        if op == "hello":
            self.reply(rid, {"start": self.start_path, "native": True, "version": VERSION, "frameless": False,
                             "max": self.is_maximized(), "linux": True, "zoom": float(self.prefs.get("Zoom", 1.15)),
                             "isDefault": is_default_file_manager()})
            self.page_ready = True
            for msg in self.pending_open:
                self.send_page(msg)
            self.pending_open = []
            return self.send_copies(True)
        if op == "win":
            act = str(m.get("action") or "")
            if act == "min":
                self.iconify()
            elif act == "max":
                self.unmaximize() if self.is_maximized() else self.maximize()
            elif act == "close":
                self.reply(rid, True)
                return self.close()
            return self.reply(rid, True)
        self.reply(rid, error="Unknown request: " + op)

    # ---- Network shares -----------------------------------------------------------------------------
    # Connected through GVFS, as you, the same as the file manager's "Connect to server": the share
    # shows under /run/user/<uid>/gvfs for every app, and a remembered password goes in the keyring.
    def smb_mount(self, rid, m):
        uri = str(m.get("uri") or "")
        if not uri.split("://")[0] in ("smb", "sftp", "ftp", "ftps", "dav", "davs"):
            return self.reply(rid, error="Type an address, such as //server/share or sftp://server.")
        user, domain, password = str(m.get("user") or ""), str(m.get("domain") or ""), str(m.get("password") or "")
        f = Gio.File.new_for_uri(uri)
        op = Gio.MountOperation()
        asked = {"n": 0, "why": ""}

        def on_ask(mo, _msg, _default_user, _default_domain, flags):
            asked["n"] += 1
            # asked again means the server turned down what was given
            if asked["n"] > 1:
                asked["why"] = "badpass"
                return mo.reply(Gio.MountOperationResult.ABORTED)
            if not user and not password and flags & Gio.AskPasswordFlags.ANONYMOUS_SUPPORTED:
                mo.set_anonymous(True)
                return mo.reply(Gio.MountOperationResult.HANDLED)
            if not user and not password:
                asked["why"] = "needpass"
                return mo.reply(Gio.MountOperationResult.ABORTED)
            mo.set_username(user)
            mo.set_domain(domain or _default_domain or "")
            mo.set_password(password)
            mo.set_password_save(Gio.PasswordSave.PERMANENTLY if m.get("remember") else Gio.PasswordSave.NEVER)
            mo.reply(Gio.MountOperationResult.HANDLED)

        op.connect("ask-password", on_ask)
        # a question (an SSH server's key seen for the first time) is put to the person, through the page
        def on_question(mo, message, choices):
            self.qseq += 1
            self.questions[self.qseq] = mo
            self.send_page(to_json({"ev": "mountq", "id": self.qseq, "message": message, "choices": list(choices)}))
        op.connect("ask-question", on_question)

        def done(_f, res):
            try:
                f.mount_enclosing_volume_finish(res)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                    if asked["why"]:
                        return self.reply(rid, {"need": asked["why"]})
                    # GVFS's own words are about the mount; say what it means for the share
                    u = urllib.parse.urlsplit(uri)
                    share = urllib.parse.unquote(u.path.strip("/").split("/")[0])
                    msg = e.message
                    if "No such file" in msg:
                        msg = u.hostname + " has no share called " + share + "."
                    elif any(w in msg for w in ("Invalid argument", "Connection refused", "Host is down", "No route", "timed out", "Connection timed")):
                        msg = "Couldn't reach " + (u.hostname or "the server") + ". Check the name, and that it is on."
                    elif "not mountable" in msg and u.scheme in ("dav", "davs"):
                        msg = "WebDAV needs one more part of GVFS. Install it with: sudo pacman -S gvfs-dnssd"
                    elif msg.strip() == "Connection failed":
                        msg = "Couldn't connect to " + (u.hostname or "the server") + ". Check the name, and that it is on."
                    elif "Permission denied" in msg:
                        msg = "The server turned down that user name and password."
                    return self.reply(rid, error=msg)
            path = f.get_path()
            if not path:
                return self.reply(rid, error="The share connected, but it has no folder to open (is gvfsd-fuse running?).")
            self.reply(rid, {"path": path, "uri": uri})

        f.mount_enclosing_volume(Gio.MountMountFlags.NONE, op, None, done)

    # ---- Drives that are plugged in: mounting them, and ejecting them ---------------------------------
    def on_volumes(self, *_a):
        if self.vols_t:
            GLib.source_remove(self.vols_t)
        self.vols_t = GLib.timeout_add(500, self.send_volumes)

    def send_volumes(self):
        self.vols_t = 0
        self.send_page('{"ev":"drives"}')
        return False

    def _vol_id(self, v):
        return v.get_identifier("unix-device") or v.get_identifier("uuid") or v.get_uuid() or v.get_name()

    def volumes(self):
        """Drives plugged in but not mounted (a USB stick that hasn't been opened yet, a second disk)."""
        out = []
        for v in self.vmon.get_volumes():
            if v.get_mount() or not v.can_mount():
                continue
            d = v.get_drive()
            out.append({"id": self._vol_id(v), "name": v.get_name(), "removable": bool(d and (d.is_removable() or d.is_media_removable())),
                        "device": v.get_identifier("unix-device") or ""})
        return {"volumes": out}

    def mount_volume(self, rid, vid):
        v = next((x for x in self.vmon.get_volumes() if self._vol_id(x) == vid), None)
        if not v:
            return self.reply(rid, error="That drive is no longer there.")

        def done(_v, res):
            try:
                v.mount_finish(res)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                    return self.reply(rid, error=e.message)
            mnt = v.get_mount()
            self.reply(rid, {"path": mnt.get_root().get_path() if mnt else ""})

        v.mount(Gio.MountMountFlags.NONE, Gio.MountOperation(), None, done)

    def eject(self, rid, path, force):
        try:
            mnt = Gio.File.new_for_path(path).find_enclosing_mount(None)
        except GLib.Error as e:
            return self.reply(rid, error=e.message)
        flags = Gio.MountUnmountFlags.FORCE if force else Gio.MountUnmountFlags.NONE
        can_eject = mnt.can_eject()

        def done(_m, res):
            try:
                if can_eject:
                    mnt.eject_with_operation_finish(res)
                else:
                    mnt.unmount_with_operation_finish(res)
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.BUSY) or "busy" in e.message.lower():
                    return self.reply(rid, {"busy": True, "why": e.message})
                return self.reply(rid, error=e.message)
            self.reply(rid, {"ejected": can_eject})

        if can_eject:
            mnt.eject_with_operation(flags, None, None, done)
        else:
            mnt.unmount_with_operation(flags, None, None, done)

    def smb_unmount(self, rid, path, force):
        try:
            mnt = Gio.File.new_for_path(path).find_enclosing_mount(None)
        except GLib.Error as e:
            return self.reply(rid, error=e.message)

        def done(_m, res):
            try:
                mnt.unmount_with_operation_finish(res)
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.BUSY):
                    return self.reply(rid, {"busy": True, "why": e.message})
                return self.reply(rid, error=e.message)
            self.reply(rid, True)

        flags = Gio.MountUnmountFlags.FORCE if force else Gio.MountUnmountFlags.NONE
        mnt.unmount_with_operation(flags, None, None, done)

    # ---- Handing things to the desktop --------------------------------------------------------------
    def open_with(self, rid, path):
        f = Gio.File.new_for_path(path)
        dlg = Gtk.AppChooserDialog.new(self, Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT, f)
        if dlg.run() == Gtk.ResponseType.OK:
            info = dlg.get_app_info()
            if info:
                try:
                    info.launch([f], None)
                except GLib.Error as e:
                    dlg.destroy()
                    return self.reply(rid, error=e.message)
        dlg.destroy()
        self.reply(rid, True)

    def reveal(self, path):
        uri = Gio.File.new_for_path(path).get_uri()
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1",
                          "ShowItems", GLib.Variant("(ass)", ([uri], "")), None, Gio.DBusCallFlags.NONE, 3000, None)
        except GLib.Error:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(os.path.dirname(path)).get_uri(), None)

    def terminal(self, path):
        cmd = None
        if shutil.which("xdg-terminal-exec"):
            cmd = ["xdg-terminal-exec", "--dir=" + path]
        else:
            for t in (os.environ.get("TERMINAL"), "alacritty", "ghostty", "kitty", "foot", "gnome-terminal", "konsole", "xterm"):
                if t and shutil.which(t):
                    cmd = [t]
                    break
        if not cmd:
            raise RuntimeError("No terminal was found.")
        if shutil.which("uwsm"):
            cmd = ["uwsm", "app", "--"] + cmd
        subprocess.Popen(cmd, cwd=path, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # copied files are offered the way GNOME's Files offers them, so a paste there (or here) works
    def clip_set(self, paths, cut):
        uris = [Gio.File.new_for_path(p).get_uri() for p in paths]
        if shutil.which("wl-copy") and os.environ.get("WAYLAND_DISPLAY"):
            body = ("cut" if cut else "copy") + "\n" + "\n".join(uris)
            subprocess.run(["wl-copy", "--type", "x-special/gnome-copied-files"], input=body.encode(), timeout=3)
        else:
            cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            cb.set_text("\n".join(paths), -1)
            cb.store()

    def clip_get(self):
        if not (shutil.which("wl-paste") and os.environ.get("WAYLAND_DISPLAY")):
            return {"paths": []}
        try:
            types = subprocess.run(["wl-paste", "--list-types"], capture_output=True, timeout=2).stdout.decode().split()
        except (subprocess.SubprocessError, OSError):
            return {"paths": []}
        for t in ("x-special/gnome-copied-files", "text/uri-list"):
            if t not in types:
                continue
            try:
                out = subprocess.run(["wl-paste", "--no-newline", "--type", t], capture_output=True, timeout=2).stdout.decode()
            except (subprocess.SubprocessError, OSError):
                return {"paths": []}
            lines = [ln.strip() for ln in out.replace("\r", "").split("\n") if ln.strip()]
            cut = False
            if t == "x-special/gnome-copied-files" and lines and lines[0] in ("cut", "copy"):
                cut = lines.pop(0) == "cut"
            paths = [Gio.File.new_for_uri(u).get_path() for u in lines if u.startswith("file://")]
            return {"paths": [p for p in paths if p], "cut": cut}
        return {"paths": []}

    def save_text(self, rid, name, text):
        dlg = Gtk.FileChooserNative.new("Save as", self, Gtk.FileChooserAction.SAVE, "_Save", "_Cancel")
        dlg.set_current_name(name)
        dlg.set_do_overwrite_confirmation(True)
        docs = core.xdg_dir("DOCUMENTS")
        if os.path.isdir(docs):
            dlg.set_current_folder(docs)
        if dlg.run() != Gtk.ResponseType.ACCEPT:
            return self.reply(rid, {"cancelled": True})
        path = dlg.get_filename()
        # with a byte-order mark, so spreadsheets read names with accents correctly
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(text)
        self.reply(rid, {"path": path})

    # ---- Watching open folders for changes ------------------------------------------------------------
    def watch(self, paths):
        want = set(paths)
        for p in list(self.monitors):
            if p not in want:
                self.monitors.pop(p).cancel()
        for p in want:
            if p in self.monitors or len(self.monitors) >= 16 or not os.path.isdir(p):
                continue
            try:
                mon = Gio.File.new_for_path(p).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            except GLib.Error:
                continue
            mon.set_rate_limit(400)
            mon.connect("changed", lambda *_a, me=p: self.changed.add(me))
            self.monitors[p] = mon

    def on_omarchy_changed(self, *_a):
        # a theme change rewrites several files here; tell the page once they have settled
        if self.omarchy_t:
            GLib.source_remove(self.omarchy_t)
        self.omarchy_t = GLib.timeout_add(400, self.send_omarchy)

    def send_omarchy(self):
        self.omarchy_t = 0
        pal = omarchy_palette()
        if pal:
            self.send_page(to_json({"ev": "omarchy", "palette": pal}))
        return False

    # ---- The copy queue: one copy or move at a time, in order -----------------------------------------
    def start_next_copy(self):
        if any(c.status == "running" for c in self.copies):
            return
        for c in self.copies:
            if c.status == "waiting":
                c.status = "running"
                c.started = time.monotonic()
                c.job = Job(0, "copy", c.args)
                c.job.state = c.state
                c.job.thread.start()
                return

    def send_copies(self, force):
        j = to_json([c.as_row() for c in self.copies])
        if force or j != self.copies_sent:
            self.copies_sent = j
            self.send_page('{"ev":"copies","list":' + j + "}")

    # ---- The timer: passes on background results and progress, copy progress and folder changes -------
    def tick(self):
        for rid in list(self.jobs):
            j = self.jobs[rid]
            self.drain(j)
            if j.done:
                self.drain(j)
                res = j.result
                if isinstance(res, dict) and res.get("__error"):
                    self.reply(rid, error=res["__error"])
                elif j.error:
                    self.reply(rid, error=j.error)
                else:
                    self.reply(rid, res)
                del self.jobs[rid]
        changed = False
        for c in self.copies:
            if c.status != "running" or not c.job.done:
                continue
            if c.job.error:
                c.status, c.error = "failed", c.job.error
            else:
                r = c.job.result or {}
                c.result = r
                c.status = "cancelled" if r.get("cancelled") else ("problems" if r.get("problems") else "done")
            c.ended = time.monotonic()
            changed = True
            # the folder it went into, and the ones it came from, are now different
            self.changed.add(c.args["dest"])
            for s in c.args["sources"]:
                self.changed.add(os.path.dirname(str(s).rstrip("/")))
        if changed:
            self.start_next_copy()
            self.send_copies(True)
        elif time.monotonic() - self.last_copy_send > 0.25:
            if any(c.status == "running" for c in self.copies):
                self.send_copies(False)
            self.last_copy_send = time.monotonic()
        # folders that changed on disk (each told once in a short while, however many files changed)
        if self.changed:
            now = time.monotonic()
            keep = set()
            for p in self.changed:
                if now - self.changed_sent.get(p, 0) < 0.4:
                    keep.add(p)
                    continue
                self.changed_sent[p] = now
                self.send_page('{"ev":"changed","path":' + to_json(p) + "}")
            self.changed = keep
        return True

    def drain(self, j):
        q = j.state.events
        while not q.empty():
            self.send_page('{"ev":"job","id":%d,"data":%s}' % (j.id, to_json(q.get())))

    # ---- Files for the preview pane and file-type icons, served to the page ----------------------------
    # The page asks for atlas://app/file?p=<path> (a picture, a PDF, a video...) and atlas://app/icon?e=.docx.
    def answer_resource(self, req):
        u = urllib.parse.urlsplit(req.get_uri())
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/atlas.html", "/", ""):
                with open(PAGE, "rb") as f:
                    data = f.read()
                return self.finish(req, data, "text/html; charset=utf-8")
            if u.path == "/icon":
                png = self.icon_png((q.get("e") or q.get("p") or [""])[0], (q.get("s") or [""])[0] == "l")
                if not png:
                    return req.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), "no icon", Gio.IOErrorEnum.NOT_FOUND))
                return self.finish(req, png, "image/png")
            if u.path == "/thumb":
                p = (q.get("p") or [""])[0]
                size = max(64, min(1600, int((q.get("s") or ["256"])[0] or 256)))
                threading.Thread(target=self.make_thumb, args=(req, p, size), daemon=True).start()
                return
            if u.path == "/file":
                p = (q.get("p") or [""])[0]
                if not p or core.kind(p) != 1:
                    return req.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), "not found", Gio.IOErrorEnum.NOT_FOUND))
                ext = os.path.splitext(p)[1].lower()
                stream = Gio.File.new_for_path(p).read(None)
                return self.finish(req, stream, TYPES.get(ext, "application/octet-stream"), os.path.getsize(p))
        except (OSError, GLib.Error) as e:
            return req.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), str(e), Gio.IOErrorEnum.FAILED))
        req.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), "not found", Gio.IOErrorEnum.NOT_FOUND))

    # ---- Thumbnails: a picture of a video, the first page of a PDF, a small copy of a photo -----------
    # Made in the background (never on the window's thread) and kept in ~/.cache/atlas/thumbs.
    THUMB_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "atlas", "thumbs")
    thumb_gate = threading.BoundedSemaphore(3)
    VIDEO = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".3gp", ".ogv")

    def make_thumb(self, req, p, size):
        import hashlib
        png, err = None, "no thumbnail"
        try:
            st = os.stat(p)
            key = hashlib.sha1(("%s|%d|%d|%d" % (p, st.st_mtime_ns, st.st_size, size)).encode()).hexdigest()
            out = os.path.join(self.THUMB_DIR, key + ".png")
            if not os.path.exists(out):
                os.makedirs(self.THUMB_DIR, exist_ok=True)
                tmp = out + ".part"
                ext = os.path.splitext(p)[1].lower()
                with self.thumb_gate:
                    if ext in self.VIDEO and shutil.which("ffmpegthumbnailer"):
                        subprocess.run(["ffmpegthumbnailer", "-i", p, "-o", tmp, "-s", str(size), "-c", "png", "-q", "8"],
                                       capture_output=True, timeout=30)
                    elif ext == ".pdf" and shutil.which("pdftoppm"):
                        subprocess.run(["pdftoppm", "-png", "-singlefile", "-f", "1", "-l", "1", "-scale-to", str(size), p, tmp[:-4]],
                                       capture_output=True, timeout=30)
                        if os.path.exists(tmp[:-4] + ".png"):
                            os.replace(tmp[:-4] + ".png", tmp)
                    else:
                        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(p, size, size, True)
                        pb = pb.apply_embedded_orientation() or pb
                        pb.savev(tmp, "png", [], [])
                if os.path.exists(tmp) and os.path.getsize(tmp):
                    os.replace(tmp, out)
            if os.path.exists(out):
                with open(out, "rb") as f:
                    png = f.read()
        except Exception as e:  # a thumbnail that can't be made just isn't shown
            err = str(e) or err
        GLib.idle_add(self._thumb_done, req, png, err)

    def _thumb_done(self, req, png, err):
        if png:
            self.finish(req, png, "image/png")
        else:
            req.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), err, Gio.IOErrorEnum.NOT_FOUND))
        return False

    def finish(self, req, data, ctype, length=None):
        if isinstance(data, bytes):
            length = len(data)
            data = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
        if hasattr(WebKit2, "URISchemeResponse"):
            resp = WebKit2.URISchemeResponse.new(data, length)
            resp.set_content_type(ctype)
            hdr = WebKit2.SoupMessageHeaders.new(WebKit2.SoupMessageHeadersType.RESPONSE) if hasattr(WebKit2, "SoupMessageHeaders") else None
            if hdr is None:
                try:
                    gi.require_version("Soup", "3.0")
                    from gi.repository import Soup
                    hdr = Soup.MessageHeaders.new(Soup.MessageHeadersType.RESPONSE)
                except (ValueError, ImportError):
                    hdr = None
            if hdr is not None:
                hdr.append("Cache-Control", "max-age=86400" if ctype == "image/png" else "no-store")
                resp.set_http_headers(hdr)
            req.finish_with_response(resp)
        else:
            req.finish(data, length, ctype)

    def icon_png(self, ext, large):
        """The icon theme's icon for a file type, as a PNG"""
        key = ("L" if large else "S") + ext.lower()
        if key in self.icon_cache:
            return self.icon_cache[key]
        png = None
        try:
            ctype, _ = Gio.content_type_guess("file" + ext.lower(), None)
            gicon = Gio.content_type_get_icon(ctype)
            theme = Gtk.IconTheme.get_default()
            size = 96 if large else 32
            info = theme.lookup_by_gicon(gicon, size, Gtk.IconLookupFlags.FORCE_SIZE)
            if info is None:
                info = theme.lookup_icon("text-x-generic", size, Gtk.IconLookupFlags.FORCE_SIZE)
            if info is not None:
                ok, buf = info.load_icon().save_to_bufferv("png", [], [])
                if ok:
                    png = bytes(buf)
        except GLib.Error:
            png = None
        if len(self.icon_cache) < 4000:
            self.icon_cache[key] = png
        return png

    # ---- Only Atlas's own page: anything else opens in the usual browser --------------------------------
    def on_policy(self, _view, decision, kind):
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if not uri.startswith("atlas://app/"):
                decision.ignore()
                if uri.startswith(("http://", "https://")):
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                return True
        return False

    # ---- The window --------------------------------------------------------------------------------------
    def on_state(self, _w, ev):
        now = bool(ev.new_window_state & Gdk.WindowState.MAXIMIZED)
        if now != self.was_max:
            self.was_max = now
            self.send_page('{"ev":"win","max":%s}' % ("true" if now else "false"))
        return False

    def open_folder(self, path, select=None, props=False):
        self.present()
        if path and os.path.exists(path) and not os.path.isdir(path) and select is None:
            path, select = os.path.dirname(os.path.abspath(path)), [os.path.basename(path)]
        if path:
            msg = to_json({"ev": "open", "path": os.path.abspath(path), "select": select or [], "props": props})
            if self.page_ready:
                self.send_page(msg)
            else:
                self.pending_open.append(msg)

    def on_close(self, *_a):
        # a copy still going: ask first, as closing stops it
        if any(c.status in ("running", "waiting") for c in self.copies):
            dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO,
                                    text="A copy is still going.")
            dlg.format_secondary_text("Closing Atlas stops it, and the file being copied is not kept.\n\nClose anyway?")
            ans = dlg.run()
            dlg.destroy()
            if ans != Gtk.ResponseType.YES:
                return True
        for c in self.copies:
            c.state.cancel, c.state.paused = True, False
        for j in self.jobs.values():
            j.state.cancel = True
        if not self.is_maximized():
            w, h = self.get_size()
            self.prefs["W"], self.prefs["H"] = w, h
        self.prefs["Max"] = self.is_maximized()
        # a picker leaves Atlas's saved window size and zoom as they were
        if not self.pick:
            try:
                with open(self.prefs_path, "w") as f:
                    json.dump(self.prefs, f)
            except OSError:
                pass
        for mon in self.monitors.values():
            mon.cancel()
        # give a cancelled copy a moment to remove its part-copied file
        until = time.monotonic() + 2
        while time.monotonic() < until and any(c.status == "running" and not c.job.done for c in self.copies):
            time.sleep(0.1)
        return False


def is_default_file_manager():
    """Whether folders open in Atlas (it is the handler for inode/directory)."""
    try:
        app = Gio.AppInfo.get_default_for_type("inode/directory", False)
        return bool(app) and app.get_id() == "atlas.desktop"
    except GLib.Error:
        return False


# org.freedesktop.FileManager1: what browsers and other apps call for "Show in folder"
FM1_XML = """<node><interface name="org.freedesktop.FileManager1">
  <method name="ShowFolders"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
  <method name="ShowItems"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
  <method name="ShowItemProperties"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
</interface></node>"""


class AtlasApp(Gtk.Application):
    """Atlas runs once per person. Opened again (from the launcher, or with a folder), it passes the folder
    to the window already open, which comes to the front and opens the folder in a new tab."""

    def __init__(self, pick=None):
        # a picker is a window of its own, apart from the Atlas that may be open
        flags = Gio.ApplicationFlags.HANDLES_COMMAND_LINE | (Gio.ApplicationFlags.NON_UNIQUE if pick else 0)
        super().__init__(application_id=APP_ID, flags=flags)
        self.win = None
        self.fm1 = 0
        self.pick = pick

    def do_startup(self):
        Gtk.Application.do_startup(self)
        if self.pick:
            return
        # only the Atlas that is running answers; a second one passes its folder on and leaves
        self.fm1 = Gio.bus_own_name(Gio.BusType.SESSION, "org.freedesktop.FileManager1", Gio.BusNameOwnerFlags.NONE,
                                    self.on_bus, None, None)

    def on_bus(self, conn, _name):
        info = Gio.DBusNodeInfo.new_for_xml(FM1_XML).interfaces[0]
        try:
            conn.register_object("/org/freedesktop/FileManager1", info, self.on_fm1, None, None)
        except GLib.Error:
            pass

    def on_fm1(self, _conn, _sender, _path, _iface, method, params, invocation):
        uris = params.unpack()[0]
        paths = [Gio.File.new_for_uri(u).get_path() if "://" in u else u for u in uris]
        paths = [p for p in paths if p]
        win = self.window_for("")
        for p in paths:
            if method == "ShowFolders":
                win.open_folder(p, select=[])
            else:
                win.open_folder(os.path.dirname(p.rstrip("/")) or "/", select=[os.path.basename(p.rstrip("/"))], props=method == "ShowItemProperties")
        invocation.return_value(None)

    def window_for(self, path):
        if not self.win:
            self.win = AtlasWindow(self, path, self.pick)
            self.win.show_all()
        return self.win

    def do_command_line(self, cmd):
        if self.pick:
            self.window_for("")   # the page starts in the folder asked for, or the last one picked from
            return 0
        args = cmd.get_arguments()[1:]
        cwd = cmd.get_cwd() or os.getcwd()
        path = ""
        if args:
            a = args[0]
            if a.startswith("file://"):
                a = Gio.File.new_for_uri(a).get_path() or ""
            path = os.path.normpath(os.path.join(cwd, os.path.expanduser(a))) if a else ""
        if self.win:
            self.win.open_folder(path)
        else:
            self.window_for(path)
        return 0


if __name__ == "__main__":
    pick = None
    if len(sys.argv) > 2 and sys.argv[1] == "--pick":
        try:
            pick = json.loads(sys.argv[2])
        except ValueError:
            sys.exit("--pick needs JSON, like '{\"mode\": \"open\"}'")
        sys.argv = sys.argv[:1]
    # the window's app id, as Hyprland and the launcher see it (a picker has its own, so it can float)
    GLib.set_prgname(APP_ID + ".Picker" if pick else APP_ID)
    GLib.set_application_name("Atlas")
    sys.exit(AtlasApp(pick).run(sys.argv))
