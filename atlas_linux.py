"""
Atlas on Linux: what atlas.py asks of the desktop here. GTK and GIO do the work (drives and network
shares through GVFS, the clipboard, the icon theme, "Open with", the Omarchy theme), always on GTK's own
thread, the one pywebview's window runs on: requests from the page are handed to it with call_main.

The same few names (Desktop, DATA_DIR, CACHE_DIR, LINUX) are what a desktop for macOS or Windows gives.
"""

import os
import shutil
import sqlite3
import subprocess
import threading
import urllib.parse

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, WebKit2  # noqa: E402

import atlas_core as core  # noqa: E402

LINUX = True
APP_ID = "dev.atlas.Atlas"
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "atlas")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "atlas")

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


def is_default_file_manager():
    """Whether folders open in Atlas (it is the handler for inode/directory)."""
    try:
        app = Gio.AppInfo.get_default_for_type("inode/directory", False)
        return bool(app) and app.get_id() == "atlas.desktop"
    except GLib.Error:
        return False


def legacy_state():
    """The page's saved choices from Atlas 1.1 and before, which kept them in WebKitGTK's own storage."""
    db = os.path.join(DATA_DIR, "profile", "localstorage", "atlas_app_0.localstorage")
    if not os.path.isfile(db):
        return None
    try:
        con = sqlite3.connect("file:" + urllib.parse.quote(db) + "?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM ItemTable WHERE key = 'fileDesk.v1'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    v = row[0]
    return v.decode("utf-16-le") if isinstance(v, bytes) else str(v)


# org.freedesktop.FileManager1: what browsers and other apps call for "Show in folder"
FM1_XML = """<node><interface name="org.freedesktop.FileManager1">
  <method name="ShowFolders"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
  <method name="ShowItems"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
  <method name="ShowItemProperties"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
</interface></node>"""

# Atlas's own: a second Atlas started with a folder hands it to the one already open, and leaves
ATLAS_XML = """<node><interface name="dev.atlas.Atlas">
  <method name="Open"><arg type="s" name="Path" direction="in"/></method>
</interface></node>"""
ATLAS_PATH = "/dev/atlas/Atlas"


def hand_over(path):
    """Passes the folder to an Atlas that is already running. False when none is."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(APP_ID, ATLAS_PATH, APP_ID, "Open", GLib.Variant("(s)", (path,)), None,
                      Gio.DBusCallFlags.NO_AUTO_START, 3000, None)
        return True
    except GLib.Error:
        return False


def start_path(arg, cwd):
    """A folder (or file, or file:// URI) from the command line, as an absolute path."""
    if arg.startswith("file://"):
        arg = Gio.File.new_for_uri(arg).get_path() or ""
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(arg))) if arg else ""


class Desktop:
    def __init__(self, host):
        self.host = host
        self.view = self.gtkwin = None
        self.monitors = {}
        self.omarchy_t = 0
        self.icon_cache = {}
        self.questions, self.qseq = {}, 0
        self.vmon = None
        self.vols_t = 0

    # ---- GTK's thread ---------------------------------------------------------------------------------
    def call_main(self, fn, *a):
        """Runs fn on GTK's thread, soon, in the order asked."""
        def run():
            fn(*a)
            return False
        GLib.idle_add(run)

    def call_main_wait(self, fn, *a):
        if threading.current_thread() is threading.main_thread():
            return fn(*a)
        done, out = threading.Event(), {}

        def run():
            try:
                out["v"] = fn(*a)
            finally:
                done.set()
            return False
        GLib.idle_add(run)
        done.wait(10)
        return out.get("v")

    def every(self, ms, fn):
        GLib.timeout_add(ms, fn)

    # ---- Starting up: before the window, and once it is made ------------------------------------------
    def start(self):
        if not self.host.pick:
            # only the Atlas that is running answers; see hand_over
            Gio.bus_own_name(Gio.BusType.SESSION, APP_ID, Gio.BusNameOwnerFlags.NONE,
                             lambda conn, _n: self.register(conn, ATLAS_XML, ATLAS_PATH, self.on_atlas_call), None, None)
            Gio.bus_own_name(Gio.BusType.SESSION, "org.freedesktop.FileManager1", Gio.BusNameOwnerFlags.NONE,
                             lambda conn, _n: self.register(conn, FM1_XML, "/org/freedesktop/FileManager1", self.on_fm1), None, None)
        self.omarchy_mon = self.font_mon = None
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
            pass
        # drives coming and going (a USB stick in or out, a disk mounted): the page redraws its drives
        self.vmon = Gio.VolumeMonitor.get()
        for sig in ("volume-added", "volume-removed", "volume-changed", "mount-added", "mount-removed"):
            self.vmon.connect(sig, self.on_volumes)
        Gtk.Settings.get_default().props.gtk_application_prefer_dark_theme = bool(self.host.prefs.get("Dark", True))

    def attach(self, window):
        """The window pywebview has just made (on GTK's thread, before it is shown): the parts it leaves as they are."""
        from webview.platforms.gtk import BrowserView
        bv = BrowserView.instances.get(window.uid)
        if not bv:
            return
        self.view, self.gtkwin = bv.webview, bv.window
        # how big everything is drawn: a little bigger than the page's own sizes by default, Ctrl+= and Ctrl+- change it
        self.view.set_zoom_level(float(self.host.prefs.get("Zoom", 1.15)))
        st = self.view.get_settings()
        if os.environ.get("ATLAS_DEBUG") and hasattr(st, "set_enable_write_console_messages_to_stdout"):
            st.set_enable_write_console_messages_to_stdout(True)
        st.set_enable_back_forward_navigation_gestures(False)
        self.view.connect("decide-policy", self.on_policy)
        self.view.connect("create", lambda *a: None)
        self.gtkwin.connect("window-state-event", self.on_state)

    # ---- Only Atlas's own page: anything else opens in the usual browser --------------------------------
    def on_policy(self, _view, decision, _kind):
        if isinstance(decision, WebKit2.NavigationPolicyDecision):
            uri = decision.get_navigation_action().get_request().get_uri()
            if not uri.startswith(self.host.base):
                decision.ignore()
                if uri.startswith(("http://", "https://")):
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                return True
        return False

    # ---- The window --------------------------------------------------------------------------------------
    def on_state(self, _w, ev):
        self.host.maximized(bool(ev.new_window_state & Gdk.WindowState.MAXIMIZED))
        return False

    def geometry(self):
        """The window's size (when not maximized), and whether it is maximized."""
        if not self.gtkwin:
            return None
        w, h = self.gtkwin.get_size()
        return w, h, self.gtkwin.is_maximized()

    def present(self):
        if self.gtkwin:
            self.gtkwin.present()

    def set_zoom(self, z):
        if self.view:
            self.view.set_zoom_level(z)

    def set_dark(self, dark):
        Gtk.Settings.get_default().props.gtk_application_prefer_dark_theme = dark

    def confirm(self, text, more):
        dlg = Gtk.MessageDialog(transient_for=self.gtkwin, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.YES_NO, text=text)
        dlg.format_secondary_text(more)
        ans = dlg.run()
        dlg.destroy()
        return ans == Gtk.ResponseType.YES

    def stop(self):
        for mon in self.monitors.values():
            mon.cancel()

    def is_default(self):
        return is_default_file_manager()

    # ---- D-Bus: "Show in folder" from other apps, and a second Atlas passing on its folder --------------
    def register(self, conn, xml, path, handler):
        info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
        try:
            conn.register_object(path, info, handler, None, None)
        except GLib.Error:
            pass

    def on_atlas_call(self, _conn, _sender, _path, _iface, _method, params, invocation):
        self.host.open_folder(params.unpack()[0])
        invocation.return_value(None)

    def on_fm1(self, _conn, _sender, _path, _iface, method, params, invocation):
        uris = params.unpack()[0]
        paths = [Gio.File.new_for_uri(u).get_path() if "://" in u else u for u in uris]
        for p in [p for p in paths if p]:
            if method == "ShowFolders":
                self.host.open_folder(p, select=[])
            else:
                self.host.open_folder(os.path.dirname(p.rstrip("/")) or "/", select=[os.path.basename(p.rstrip("/"))],
                                      props=method == "ShowItemProperties")
        invocation.return_value(None)

    # ---- What the page asks of the desktop ---------------------------------------------------------------
    def handle(self, rid, op, m):
        """Answers a request the desktop deals with. False when it isn't one of them."""
        reply = self.host.reply
        if op == "open":
            for p in m.get("paths") or []:
                Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(p)).get_uri(), None)
            reply(rid, True)
        elif op == "openwith":
            self.open_with(rid, str(m.get("path")))
        elif op == "reveal":
            self.reveal(str(m.get("path")))
            reply(rid, True)
        elif op == "explorer":
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(m.get("path"))).get_uri(), None)
            reply(rid, True)
        elif op == "terminal":
            self.terminal(str(m.get("path")))
            reply(rid, True)
        elif op == "smbmount":
            self.smb_mount(rid, m)
        elif op == "mountanswer":
            mo = self.questions.pop(int(m.get("id") or 0), None)
            if mo:
                ch = m.get("choice")
                if ch is None:
                    mo.reply(Gio.MountOperationResult.ABORTED)
                else:
                    mo.set_choice(int(ch))
                    mo.reply(Gio.MountOperationResult.HANDLED)
            reply(rid, True)
        elif op == "volumes":
            reply(rid, self.volumes())
        elif op == "mountvol":
            self.mount_volume(rid, str(m.get("id") or ""))
        elif op == "eject":
            self.eject(rid, str(m.get("path") or ""), bool(m.get("force")))
        elif op == "smbunmount":
            self.smb_unmount(rid, str(m.get("path") or ""), bool(m.get("force")))
        elif op == "clipset":
            self.clip_set([str(p) for p in m.get("paths") or []], bool(m.get("cut")))
            reply(rid, True)
        elif op == "clipget":
            reply(rid, self.clip_get())
        elif op == "copytext":
            cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            cb.set_text(str(m.get("text") or ""), -1)
            cb.store()
            reply(rid, True)
        elif op == "savetext":
            self.save_text(rid, str(m.get("name") or "export.csv"), str(m.get("text") or ""))
        elif op == "url":
            u = str(m.get("url") or "")
            if u.startswith("https://"):
                Gio.AppInfo.launch_default_for_uri(u, None)
            reply(rid, True)
        elif op == "omarchy":
            reply(rid, omarchy_palette())
        else:
            return False
        return True

    # ---- Network shares -----------------------------------------------------------------------------
    # Connected through GVFS, as you, the same as the file manager's "Connect to server": the share
    # shows under /run/user/<uid>/gvfs for every app, and a remembered password goes in the keyring.
    def smb_mount(self, rid, m):
        uri = str(m.get("uri") or "")
        if not uri.split("://")[0] in ("smb", "sftp", "ftp", "ftps", "dav", "davs"):
            return self.host.reply(rid, error="Type an address, such as //server/share or sftp://server.")
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
            self.host.event({"ev": "mountq", "id": self.qseq, "message": message, "choices": list(choices)})
        op.connect("ask-question", on_question)

        def done(_f, res):
            try:
                f.mount_enclosing_volume_finish(res)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                    if asked["why"]:
                        return self.host.reply(rid, {"need": asked["why"]})
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
                    return self.host.reply(rid, error=msg)
            path = f.get_path()
            if not path:
                return self.host.reply(rid, error="The share connected, but it has no folder to open (is gvfsd-fuse running?).")
            self.host.reply(rid, {"path": path, "uri": uri})

        f.mount_enclosing_volume(Gio.MountMountFlags.NONE, op, None, done)

    # ---- Drives that are plugged in: mounting them, and ejecting them ---------------------------------
    def on_volumes(self, *_a):
        if self.vols_t:
            GLib.source_remove(self.vols_t)
        self.vols_t = GLib.timeout_add(500, self.send_volumes)

    def send_volumes(self):
        self.vols_t = 0
        self.host.event({"ev": "drives"})
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
            return self.host.reply(rid, error="That drive is no longer there.")

        def done(_v, res):
            try:
                v.mount_finish(res)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                    return self.host.reply(rid, error=e.message)
            mnt = v.get_mount()
            self.host.reply(rid, {"path": mnt.get_root().get_path() if mnt else ""})

        v.mount(Gio.MountMountFlags.NONE, Gio.MountOperation(), None, done)

    def eject(self, rid, path, force):
        try:
            mnt = Gio.File.new_for_path(path).find_enclosing_mount(None)
        except GLib.Error as e:
            return self.host.reply(rid, error=e.message)
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
                    return self.host.reply(rid, {"busy": True, "why": e.message})
                return self.host.reply(rid, error=e.message)
            self.host.reply(rid, {"ejected": can_eject})

        if can_eject:
            mnt.eject_with_operation(flags, None, None, done)
        else:
            mnt.unmount_with_operation(flags, None, None, done)

    def smb_unmount(self, rid, path, force):
        try:
            mnt = Gio.File.new_for_path(path).find_enclosing_mount(None)
        except GLib.Error as e:
            return self.host.reply(rid, error=e.message)

        def done(_m, res):
            try:
                mnt.unmount_with_operation_finish(res)
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.BUSY):
                    return self.host.reply(rid, {"busy": True, "why": e.message})
                return self.host.reply(rid, error=e.message)
            self.host.reply(rid, True)

        flags = Gio.MountUnmountFlags.FORCE if force else Gio.MountUnmountFlags.NONE
        mnt.unmount_with_operation(flags, None, None, done)

    # ---- Handing things to the desktop --------------------------------------------------------------
    def open_with(self, rid, path):
        f = Gio.File.new_for_path(path)
        dlg = Gtk.AppChooserDialog.new(self.gtkwin, Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT, f)
        if dlg.run() == Gtk.ResponseType.OK:
            info = dlg.get_app_info()
            if info:
                try:
                    info.launch([f], None)
                except GLib.Error as e:
                    dlg.destroy()
                    return self.host.reply(rid, error=e.message)
        dlg.destroy()
        self.host.reply(rid, True)

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
        dlg = Gtk.FileChooserNative.new("Save as", self.gtkwin, Gtk.FileChooserAction.SAVE, "_Save", "_Cancel")
        dlg.set_current_name(name)
        dlg.set_do_overwrite_confirmation(True)
        docs = core.user_dir("DOCUMENTS")
        if os.path.isdir(docs):
            dlg.set_current_folder(docs)
        if dlg.run() != Gtk.ResponseType.ACCEPT:
            return self.host.reply(rid, {"cancelled": True})
        path = dlg.get_filename()
        # with a byte-order mark, so spreadsheets read names with accents correctly
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(text)
        self.host.reply(rid, {"path": path})

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
            mon.connect("changed", lambda *_a, me=p: self.host.changed.add(me))
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
            self.host.event({"ev": "omarchy", "palette": pal})
        return False

    # ---- File-type icons and thumbnails, for the page (asked for by atlas.py's server, off GTK's thread) --
    def icon_png(self, ext, large, own=False):
        """The icon theme's icon for a file type, as a PNG (own, a file's own icon, is Windows')"""
        return self.call_main_wait(self._icon_png, ext, large)

    def _icon_png(self, ext, large):
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

    # A picture of a video, the first page of a PDF, a small copy of a photo: made into tmp (a PNG)
    def make_thumb(self, p, ext, size, tmp, video):
        if video and shutil.which("ffmpegthumbnailer"):
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
