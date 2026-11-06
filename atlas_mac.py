"""
Atlas on macOS: what atlas.py asks of the desktop here. pywebview's window is Cocoa with WKWebView,
reached through pyobjc, so the clipboard, the Save panel and the icons are AppKit's; Finder's own tools
(open, osascript, diskutil, qlmanage) do the rest. Requests from the page are worked through one at a
time on a thread of Atlas's own (call_main, from atlas_worker.py); anything that must touch the window
or AppKit is handed to the main thread (ui).

The same few names (Desktop, DATA_DIR, CACHE_DIR, SYSTEM) are what atlas_linux.py gives on Linux.
"""

import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
from multiprocessing.connection import Client, Listener

import atlas_core as core
from atlas_worker import Worker

SYSTEM = "mac"
DATA_DIR = os.path.expanduser("~/Library/Application Support/Atlas")
CACHE_DIR = os.path.expanduser("~/Library/Caches/Atlas")
# a second Atlas (started from a terminal with a folder) hands it to the one already open, through this
SOCKET = os.path.join(DATA_DIR, "atlas.sock")
CUT_TYPE = "dev.atlas.Atlas.cut"   # marks files on the clipboard as cut, for a paste in Atlas (Finder has no cut)


def _key():
    """The secret both ends of the socket know: kept in Atlas's folder, readable by you alone."""
    os.makedirs(DATA_DIR, exist_ok=True)
    p = os.path.join(DATA_DIR, "socket.key")
    try:
        with open(p, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_bytes(32)
    with open(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(key)
    return key


def hand_over(path):
    """Passes the folder to an Atlas that is already running. False when none is."""
    try:
        c = Client(SOCKET, family="AF_UNIX", authkey=_key())
    except (OSError, EOFError):
        return False
    try:
        c.send(path)
        return True
    except (OSError, EOFError):
        return False
    finally:
        c.close()


def start_path(arg, cwd):
    """A folder (or file, or file:// URL) from the command line, as a full path."""
    if arg.startswith("file://"):
        arg = urllib.parse.unquote(urllib.parse.urlsplit(arg).path)
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(arg))) if arg else ""


def osascript(script):
    """Runs AppleScript given on stdin (so nothing in it, a password say, shows in the list of programs)."""
    return subprocess.run(["/usr/bin/osascript", "-"], input=script, capture_output=True, text=True, timeout=300)


def applescript_text(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def AppHelper_call(fn):
    from PyObjCTools import AppHelper
    AppHelper.callAfter(fn)


_DragSource = None


def drag_source(ended):
    """An NSDraggingSource that offers copy or move (Finder picks, as it does for its own drags) and says how it ended."""
    global _DragSource
    if _DragSource is None:
        import AppKit
        import objc

        try:
            protos = [objc.protocolNamed("NSDraggingSource")]
        except Exception:
            protos = []

        class AtlasDragSource(AppKit.NSObject, protocols=protos):
            def draggingSession_sourceOperationMaskForDraggingContext_(self, session, context):
                return AppKit.NSDragOperationCopy | AppKit.NSDragOperationMove | AppKit.NSDragOperationGeneric

            def draggingSession_endedAtPoint_operation_(self, session, point, operation):
                ended = getattr(self, "ended", None)
                if ended:
                    ended(operation)
        _DragSource = AtlasDragSource
    src = _DragSource.alloc().init()
    src.ended = ended
    return src


class Desktop(Worker):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.window = None
        self.watched = {}
        self.volumes = None
        self.icon_cache = {}

    # ---- The main thread, where AppKit and the window live -------------------------------------------
    def view(self):
        from webview.platforms.cocoa import BrowserView
        return BrowserView.instances.get(self.window.uid) if self.window else None

    def ui(self, fn):
        """Runs fn on the main thread and returns what it gave."""
        if threading.current_thread() is threading.main_thread():
            return fn()
        from PyObjCTools import AppHelper
        done, out = threading.Event(), {}

        def run():
            try:
                out["v"] = fn()
            except Exception as e:
                out["e"] = e
            finally:
                done.set()
        AppHelper.callAfter(run)
        done.wait(30)
        if "e" in out:
            raise out["e"]
        return out.get("v")

    # ---- Starting up: before the window, and once it is made ------------------------------------------
    def start(self):
        self.start_worker()
        if not self.host.pick and not self.host.extra:
            threading.Thread(target=self.listen, daemon=True).start()
        # pywebview notes where files dropped in from Finder are only while something listens for drops
        from webview.dom import _dnd_state
        _dnd_state["num_listeners"] += 1
        self.every(2000, self.check_drives)
        self.every(1000, self.check_watched)

    def attach(self, window):
        self.window = window
        window.events.maximized += lambda: self.call_main(self.host.maximized, True)
        window.events.restored += lambda: self.call_main(self.host.maximized, False)
        window.events.loaded += lambda: self.set_zoom(float(self.host.prefs.get("Zoom", 1.15)))

    def listen(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            os.unlink(SOCKET)   # left by an Atlas that didn't close (one running would have taken the hand-over)
        except OSError:
            pass
        try:
            ln = Listener(SOCKET, family="AF_UNIX", authkey=_key())
            os.chmod(SOCKET, 0o600)
        except OSError:
            return
        while True:
            try:
                c = ln.accept()
            except Exception:  # someone without the key, or a broken connection
                continue
            try:
                self.call_main(self.host.open_folder, str(c.recv() or ""))
            except Exception:
                pass
            finally:
                c.close()

    # ---- The window --------------------------------------------------------------------------------------
    def geometry(self):
        """The window's size (its inside, when not zoomed to fill the screen), and whether it is."""
        def get():
            w = self.view().window
            r = w.contentRectForFrameRect_(w.frame())
            return int(r.size.width), int(r.size.height), bool(w.isZoomed())
        try:
            return self.ui(get)
        except Exception:
            return None

    def present(self):
        def go():
            import AppKit
            w = self.view().window
            if w.isMiniaturized():
                w.deminiaturize_(None)
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            w.makeKeyAndOrderFront_(None)
        try:
            self.ui(go)
        except Exception:
            pass

    def set_zoom(self, z):
        def go():
            wv = self.view().webview
            if wv.respondsToSelector_("setPageZoom:"):   # macOS 11 and later
                wv.setPageZoom_(z)
        try:
            self.ui(go)
        except Exception:
            pass

    def set_dark(self, dark):
        def go():
            import AppKit
            AppKit.NSApp.setAppearance_(AppKit.NSAppearance.appearanceNamed_(
                AppKit.NSAppearanceNameDarkAqua if dark else AppKit.NSAppearanceNameAqua))
        try:
            self.ui(go)
        except Exception:
            pass

    def confirm(self, text, more):
        import AppKit
        a = AppKit.NSAlert.alloc().init()
        a.setMessageText_(text)
        a.setInformativeText_(more)
        a.addButtonWithTitle_("Close Anyway")
        a.addButtonWithTitle_("Cancel")
        return a.runModal() == AppKit.NSAlertFirstButtonReturn

    def stop(self):
        self.watched = {}

    def is_default(self):
        return False

    # ---- Drives coming and going, and open folders changing ----------------------------------------------
    def check_drives(self):
        try:
            now = sorted(os.listdir("/Volumes"))
        except OSError:
            now = []
        if self.volumes is not None and now != self.volumes:
            self.host.event({"ev": "drives"})
        self.volumes = now
        return True

    def watch(self, paths):
        # a folder's modified time changes when something in it is added, removed or renamed
        want = {p for p in paths if os.path.isdir(p)}
        self.watched = {p: self.watched.get(p) or self._stamp(p) for p in list(want)[:16]}

    def _stamp(self, p):
        try:
            return os.stat(p).st_mtime_ns
        except OSError:
            return 0

    def check_watched(self):
        for p, was in list(self.watched.items()):
            now = self._stamp(p)
            if now != was:
                self.watched[p] = now
                self.host.changed.add(p)
        return True

    # ---- What the page asks of the desktop ---------------------------------------------------------------
    def handle(self, rid, op, m):
        """Answers a request the desktop deals with. False when it isn't one of them."""
        reply = self.host.reply
        if op == "open":
            for p in m.get("paths") or []:
                subprocess.Popen(["/usr/bin/open", str(p)])
            reply(rid, True)
        elif op == "openwith":
            threading.Thread(target=self.open_with, args=(rid, str(m.get("path"))), daemon=True).start()
        elif op in ("reveal", "explorer"):
            subprocess.Popen(["/usr/bin/open", "-R" if op == "reveal" else "--", str(m.get("path"))])
            reply(rid, True)
        elif op == "terminal":
            subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(m.get("path"))])
            reply(rid, True)
        elif op == "clipset":
            self.ui(lambda: self.clip_set([str(p) for p in m.get("paths") or []], bool(m.get("cut"))))
            reply(rid, True)
        elif op == "clipget":
            reply(rid, self.ui(self.clip_get))
        elif op == "copytext":
            self.ui(lambda: self.copy_text(str(m.get("text") or "")))
            reply(rid, True)
        elif op == "savetext":
            reply(rid, self.ui(lambda: self.save_text(str(m.get("name") or "export.csv"), str(m.get("text") or ""))))
        elif op == "fulldisk":
            subprocess.Popen(["/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
            reply(rid, True)
        elif op == "url":
            u = str(m.get("url") or "")
            if u.startswith("https://"):
                subprocess.Popen(["/usr/bin/open", u])
            reply(rid, True)
        elif op == "smbmount":
            threading.Thread(target=self.smb_mount, args=(rid, m), daemon=True).start()
        elif op in ("eject", "smbunmount"):
            threading.Thread(target=self.eject, args=(rid, str(m.get("path") or ""), bool(m.get("force")), op == "eject"), daemon=True).start()
        elif op == "volumes":
            reply(rid, {"volumes": []})
        elif op == "mountanswer":
            reply(rid, True)
        elif op == "dropped":
            reply(rid, {"paths": self.dropped([str(n) for n in m.get("names") or []])})
        elif op == "dragout":
            paths = [str(p) for p in m.get("paths") or [] if os.path.exists(str(p))]
            if not paths:
                reply(rid, None)
            else:
                AppHelper_call(lambda: self.drag_out(rid, paths))
        else:
            return False
        return True

    # files dropped in from Finder: pywebview kept where they were (name, path) as the window took them
    def dropped(self, names):
        from webview.dom import _dnd_state
        held, out = _dnd_state["paths"], []
        for n in names:
            for k, (name, path) in enumerate(held):
                if urllib.parse.unquote(name) == n:
                    out.append(urllib.parse.unquote(path))
                    del held[k]
                    break
        del held[:]   # whatever wasn't asked for belonged to a drop the page didn't take
        return [p for p in out if os.path.exists(p)]

    # dragging files out of Atlas: an AppKit drag of the files themselves, so they can be dropped in Finder,
    # on the desktop, into Mail or any other app (and back into Atlas, where the page still knows the drag)
    def drag_out(self, rid, paths):
        import AppKit
        import Foundation
        done = []

        def end(op):
            if not done:
                done.append(op)
                self.host.reply(rid, {AppKit.NSDragOperationMove: "Move", AppKit.NSDragOperationCopy: "Copy"}.get(op, "None" if op == 0 else "Copy"))
        try:
            bv = self.view()
            # the button already let go: no drag, or it would drop wherever the pointer is
            if bv is None or not (AppKit.NSEvent.pressedMouseButtons() & 1):
                end(0)
                return
            win, wv = bv.window, bv.webview
            loc = win.mouseLocationOutsideOfEventStream()
            ev = AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                AppKit.NSEventTypeLeftMouseDragged, loc, 0, AppKit.NSProcessInfo.processInfo().systemUptime(),
                win.windowNumber(), None, 0, 1, 1.0)
            at = wv.convertPoint_fromView_(loc, None)
            items = []
            for k, p in enumerate(paths[:200]):
                item = AppKit.NSDraggingItem.alloc().initWithPasteboardWriter_(Foundation.NSURL.fileURLWithPath_(p))
                icon = AppKit.NSWorkspace.sharedWorkspace().iconForFile_(p)
                icon.setSize_((48, 48))
                off = min(k, 4) * 6
                item.setDraggingFrame_contents_(((at.x - 24 + off, at.y - 24 + off), (48, 48)), icon)
                items.append(item)
            self.drag_source = drag_source(end)
            wv.beginDraggingSessionWithItems_event_source_(items, ev, self.drag_source)
        except Exception:
            end(0)

    def open_with(self, rid, path):
        r = osascript('POSIX path of (choose application with prompt "Open ' + applescript_text(os.path.basename(path))[1:-1] + ' with:" as alias)')
        app = r.stdout.strip()
        if r.returncode == 0 and app:
            subprocess.Popen(["/usr/bin/open", "-a", app, path])
        self.host.reply(rid, True)

    # ---- Network shares: mounted the way Finder's "Connect to Server" does, in /Volumes ----------------
    def smb_mount(self, rid, m):
        uri = str(m.get("uri") or "")
        u = urllib.parse.urlsplit(uri)
        if u.scheme not in ("smb", "afp", "nfs", "ftp"):
            return self.host.reply(rid, error="Type an address such as //server/share (macOS connects to SMB, AFP, NFS and FTP).")
        user, password = str(m.get("user") or ""), str(m.get("password") or "")
        script = "mount volume " + applescript_text(uri)
        if user:
            script += " as user name " + applescript_text(user) + " with password " + applescript_text(password)
        before = set(core.system._mounts())
        r = osascript(script)
        if r.returncode != 0:
            msg = r.stderr.strip()
            if "-128" in msg:
                return self.host.reply(rid, {"need": "needpass"})
            if "-5023" in msg or "-6602" in msg or "authentication" in msg.lower():
                return self.host.reply(rid, {"need": "badpass"})
            return self.host.reply(rid, error="Couldn't connect to " + (u.hostname or "the server") + ". Check the name, and that it is on.")
        mounts = core.system._mounts()
        share = urllib.parse.unquote(u.path.strip("/").split("/")[0]).lower()
        new = [p for p in mounts if p not in before] or \
              [p for p, (src, _fs) in mounts.items() if src.lower().rstrip("/").endswith("/" + share)]
        if not new:
            return self.host.reply(rid, error="The share connected, but macOS didn't say where.")
        self.host.reply(rid, {"path": new[0], "uri": uri})

    def eject(self, rid, path, force, eject):
        cmd = ["/usr/sbin/diskutil", "eject" if eject else "unmount"] + (["force"] if force and not eject else []) + [path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            why = (r.stderr or r.stdout).strip()
            if not force and ("busy" in why.lower() or "in use" in why.lower() or "dissented" in why.lower()):
                return self.host.reply(rid, {"busy": True, "why": why})
            return self.host.reply(rid, error=why or "macOS couldn't eject it.")
        self.host.reply(rid, {"ejected": eject} if eject else True)

    # ---- The clipboard (AppKit's pasteboard): files as Finder puts them there -----------------------
    def clip_set(self, paths, cut):
        import AppKit
        pb = AppKit.NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.writeObjects_([AppKit.NSURL.fileURLWithPath_(p) for p in paths])
        if cut:
            pb.addTypes_owner_([CUT_TYPE], None)
            pb.setString_forType_("cut", CUT_TYPE)

    def clip_get(self):
        import AppKit
        pb = AppKit.NSPasteboard.generalPasteboard()
        urls = pb.readObjectsForClasses_options_([AppKit.NSURL], {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}) or []
        return {"paths": [str(u.path()) for u in urls], "cut": pb.stringForType_(CUT_TYPE) == "cut"}

    def copy_text(self, text):
        import AppKit
        pb = AppKit.NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, AppKit.NSPasteboardTypeString)

    def save_text(self, name, text):
        import AppKit
        panel = AppKit.NSSavePanel.savePanel()
        panel.setNameFieldStringValue_(name)
        docs = core.user_dir("DOCUMENTS")
        if os.path.isdir(docs):
            panel.setDirectoryURL_(AppKit.NSURL.fileURLWithPath_(docs))
        if panel.runModal() != AppKit.NSModalResponseOK:
            return {"cancelled": True}
        path = str(panel.URL().path())
        # with a byte-order mark, so spreadsheets read names with accents correctly
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(text)
        return {"path": path}

    # ---- File-type icons and thumbnails, for the page (asked for by atlas.py's server) ----------------------
    def icon_png(self, what, large, own=False):
        """The icon Finder shows: a file's own, or the one for a kind of file (.docx, or "folder")."""
        key = ("f:" if own else "e:") + ("L:" if large else "S:") + what.lower()
        if key not in self.icon_cache:
            try:
                png = self.ui(lambda: self._icon_png(what, 96 if large else 32, own))
            except Exception:
                png = None
            if len(self.icon_cache) < 4000:
                self.icon_cache[key] = png
        return self.icon_cache.get(key)

    def _icon_png(self, what, size, own):
        import AppKit
        ws = AppKit.NSWorkspace.sharedWorkspace()
        if own:
            img = ws.iconForFile_(what)
        elif what == "folder":
            img = ws.iconForFile_("/usr")
        else:
            img = ws.iconForFileType_(what.lstrip(".") or "public.data")
        if img is None:
            return None
        rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, size, size, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
        AppKit.NSGraphicsContext.saveGraphicsState()
        AppKit.NSGraphicsContext.setCurrentContext_(AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        img.drawInRect_fromRect_operation_fraction_(((0, 0), (size, size)), AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver, 1.0)
        AppKit.NSGraphicsContext.restoreGraphicsState()
        data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
        return bytes(data) if data else None

    # a picture of a video, the first page of a PDF, a small copy of a photo: Quick Look makes them all
    def make_thumb(self, p, ext, size, tmp, video):
        with tempfile.TemporaryDirectory(prefix="atlas-thumb-") as d:
            subprocess.run(["/usr/bin/qlmanage", "-t", "-s", str(size), "-o", d, p], capture_output=True, timeout=30)
            made = os.path.join(d, os.path.basename(p) + ".png")
            if os.path.exists(made):
                shutil.move(made, tmp)
