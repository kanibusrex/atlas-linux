#!/usr/bin/env python3
"""
Atlas: a file explorer, in a pywebview window. The counterpart of AtlasShell.ps1 on Windows.

The window here does the file work; atlas.html, beside this script, is what you see. The page asks the
window for things by message (list a folder, search, copy...), exactly as it does in WebView2 on
Windows: a small script gives the page an object that works like the window.chrome.webview it had there. Anything that can
take a while runs in a background thread, so the window never freezes, and its progress is passed back
to the page as it goes. atlas_core.py holds the file operations; atlas_linux.py what is asked of the
desktop (drives, shares, the clipboard, icons...).

The page, and the files it previews, come from a small web server of Atlas's own on 127.0.0.1, which
answers only Atlas's window: every address starts with a secret made fresh each time Atlas starts.

Run:   python3 atlas.py [folder]
       python3 atlas.py --pick '{"mode": "open"}'   a window for choosing files, for another app (see atlas_portal.py)
Its settings are kept in ~/.local/share/atlas.
"""

import hashlib
import http.server
import json
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
import urllib.parse

# ATLAS_LOG=file: errors (and with ATLAS_DEBUG, what went wrong where) go there, for the Windows app,
# which has no console. Set before anything else is loaded, so pywebview's own messages go there too, and
# a crash leaves its traceback. Not stdout: a picker gives its answer there.
if os.environ.get("ATLAS_LOG"):
    import faulthandler
    sys.stderr = open(os.environ["ATLAS_LOG"], "a", encoding="utf-8", buffering=1)
    faulthandler.enable(sys.stderr)

HERE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))   # beside it, or in the built app
sys.path.insert(0, HERE)
# pywebview, and what it needs, come with Atlas (build-appimage.sh puts them in lib)
if os.path.isdir(os.path.join(HERE, "lib")):
    sys.path.insert(1, os.path.join(HERE, "lib"))
import atlas_core as core  # noqa: E402
import atlas_update as update  # noqa: E402

if sys.platform.startswith("linux"):
    import atlas_linux as desktop  # noqa: E402
elif sys.platform == "win32":
    import atlas_windows as desktop  # noqa: E402
elif sys.platform == "darwin":
    import atlas_mac as desktop  # noqa: E402
else:
    sys.exit("Atlas runs on Linux, Windows and macOS.")

import webview  # noqa: E402

VERSION = "1.2.5"
DATA_DIR = desktop.DATA_DIR
PAGE = os.path.join(HERE, "atlas.html")
BG = "#1a1b1e"
PICK_STATE = os.path.join(DATA_DIR, "pick.json")   # the folder last picked from, for the next picker
PAGE_STATE = os.path.join(DATA_DIR, "page.json")   # the page's saved choices: favourites, tabs, theme...
THUMB_DIR = os.path.join(desktop.CACHE_DIR, "thumbs")

# what the page is given before its own script runs: __atlasHost, which works like the window.chrome.webview
# it talked to in the Windows Atlas, the system it's on, its saved choices, and where to ask for files and icons. (Not
# window.chrome.webview itself: on Windows pywebview's own messages go through the real one.)
BRIDGE = r"""
window.__atlasLinux = %(linux)s;
window.__atlasSystem = %(system)s;
window.__atlasRes = %(res)s;
(function () {
  var listeners = [], waiting = [];
  function send(s) {
    var api = window.pywebview && window.pywebview.api;
    if (api && api.post) { api.post(s); } else { waiting.push(s); }
  }
  window.addEventListener("pywebviewready", function () { var w = waiting; waiting = []; w.forEach(send); });
  window.__atlasHost = {
    postMessage: function (m) { send(JSON.stringify(m)); },
    addEventListener: function (type, fn) { if (type === "message") { listeners.push(fn); } },
    removeEventListener: function (type, fn) { listeners = listeners.filter(function (f) { return f !== fn; }); }
  };
  window.__atlasRecv = function (list) {
    list.forEach(function (d) { listeners.forEach(function (fn) { try { fn({ data: d }); } catch (e) { setTimeout(function () { throw e; }); } }); });
  };
  var saved = %(saved)s;
  window.__atlasStore = {
    get: function () { return saved; },
    set: function (v) { saved = v; send(JSON.stringify({ id: 0, op: "store", value: v })); }
  };
})();
"""

TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".bmp": "image/bmp", ".ico": "image/x-icon", ".svg": "image/svg+xml", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/mp4", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".flac": "audio/flac",
}
VIDEO = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".3gp", ".ogv")

def to_json(v):
    # all ASCII: pywebview on GTK gives WebKit a script's length in characters, where it means bytes,
    # so anything after a "…" or an accented name would be cut off
    return json.dumps(v, separators=(",", ":"), ensure_ascii=True, default=str)


def write_file(path, text):
    """Replaces a file all at once, so it is never left half-written."""
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


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
            if os.environ.get("ATLAS_DEBUG"):
                traceback.print_exc()
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


class Api:
    """What the page's script can call (as window.pywebview.api): one thing, a message for the host."""

    def __init__(self, host):
        self._host = host

    def post(self, msg):
        self._host.on_message(msg)


class Host:
    def __init__(self, start, pick=None):
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
        self.changed = set()
        self.changed_sent = {}
        self.is_max = False
        self.window = None
        self.outbox = queue.Queue()
        os.makedirs(DATA_DIR, exist_ok=True)
        self.prefs_path = os.path.join(DATA_DIR, "atlas.json")
        # UpdateCheck: look for a new version on GitHub (at most daily); UpdateLast, when it last did; UpdateSkip, one not wanted
        self.prefs = {"W": 1360, "H": 860, "Max": False, "Dark": True, "Zoom": 1.15, "UpdateCheck": True, "UpdateLast": 0, "UpdateSkip": ""}
        try:
            with open(self.prefs_path) as f:
                self.prefs.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.saved = self.load_page_state()
        # what the last update downloaded is done with once the new Atlas is running
        if not pick:
            shutil.rmtree(os.path.join(desktop.CACHE_DIR, "update"), ignore_errors=True)
        self.token = secrets.token_urlsafe(24)
        self.server = Resources(self)
        self.base = "http://127.0.0.1:%d/%s/" % (self.server.server_address[1], self.token)
        self.desktop = desktop.Desktop(self)

    def load_page_state(self):
        try:
            with open(PAGE_STATE, encoding="utf-8") as f:
                text = f.read()
            json.loads(text)
            return text
        except (OSError, ValueError):
            pass
        # kept by the browser part of an older Atlas: brought over, once
        text = getattr(desktop, "legacy_state", lambda: None)()
        if text:
            try:
                json.loads(text)
                write_file(PAGE_STATE, text)
                return text
            except (OSError, ValueError):
                pass
        return None

    def run(self):
        q = "?path=" + urllib.parse.quote(os.path.abspath(self.start_path), safe="") if self.start_path else ""
        if self.start_path and self.start_select:
            q += "&select=" + urllib.parse.quote(self.start_select, safe="")
        if self.pick:
            q += ("&" if q else "?") + "pick=" + urllib.parse.quote(json.dumps(self.pick), safe="")
        if self.pick:
            title = self.pick.get("title") or ("Save" if self.pick.get("mode") != "open" else "Open")
            w, h, maxed = min(int(self.prefs["W"]), 1100), min(int(self.prefs["H"]), 700), False
        else:
            title, w, h, maxed = "Atlas", int(self.prefs["W"]), int(self.prefs["H"]), bool(self.prefs.get("Max"))
        self.is_max = maxed
        self.window = webview.create_window(title, self.base + "atlas.html" + q, js_api=Api(self), width=w, height=h,
                                            min_size=(760, 480), maximized=maxed, background_color=BG, text_select=True)
        self.window.events.before_show += lambda: self.desktop.attach(self.window)
        self.window.events.closing += self.on_closing
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self.sender, daemon=True).start()
        self.desktop.start()
        self.desktop.every(50, self.tick)
        debug = bool(os.environ.get("ATLAS_DEBUG"))
        webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
        # Windows takes only an .ico for a window's icon (a PNG there is an error that ends Atlas)
        ico = os.path.join(HERE, "atlas.ico" if sys.platform == "win32" else "atlas.png")
        webview.start(debug=debug, private_mode=True, icon=ico if os.path.exists(ico) else None)

    # ---- Talking to the page ----------------------------------------------------------------------
    # Messages for the page wait in the outbox; one thread hands over everything waiting at once
    def send(self, obj_json):
        self.outbox.put(obj_json)

    def event(self, obj):
        self.send(to_json(obj))

    def sender(self):
        while True:
            items = [self.outbox.get()]
            while True:
                try:
                    items.append(self.outbox.get_nowait())
                except queue.Empty:
                    break
            try:
                self.window.run_js("window.__atlasRecv && window.__atlasRecv([" + ",".join(items) + "]);")
            except Exception:
                pass

    def reply(self, rid, data=None, error=""):
        if error:
            self.send('{"re":%d,"ok":false,"error":%s}' % (rid, to_json(error)))
        else:
            self.send('{"re":%d,"ok":true,"data":%s}' % (rid, to_json(data)))

    def on_message(self, msg):
        # from pywebview's thread for the call: the work is done where the desktop does its own
        try:
            m = json.loads(msg)
        except (TypeError, ValueError):
            return
        self.desktop.call_main(self.handle_message, m)

    def handle_message(self, m):
        rid = int(m.get("id") or 0)
        try:
            self.receive(rid, str(m.get("op") or ""), m)
        except Exception as e:
            if os.environ.get("ATLAS_DEBUG"):
                traceback.print_exc()
            self.reply(rid, error=str(e) or e.__class__.__name__)

    def receive(self, rid, op, m):
        # the file work: each request in a thread of its own (copies wait their turn in the copy queue, below)
        if core.handles(op) and op != "copy":
            j = Job(rid, op, m)
            self.jobs[rid] = j
            j.thread.start()
            return
        if op == "cancel":
            j = self.jobs.get(int(m.get("job") or 0))
            if j:
                j.state.cancel = True
            return self.reply(rid, True)
        if op == "store":
            # the page's saved choices (a picker keeps none of its own)
            v = m.get("value")
            if isinstance(v, str) and not self.pick:
                self.saved = v
                try:
                    write_file(PAGE_STATE, v)
                except OSError:
                    pass
            return
        if op == "properties":
            return self.reply(rid, error="Use the preview pane for an item's details.")
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
            self.desktop.watch([str(p) for p in m.get("paths") or []])
            return self.reply(rid, True)
        if op == "zoom":
            z = max(0.7, min(2.0, float(m.get("level") or 1.15)))
            self.desktop.set_zoom(z)
            self.prefs["Zoom"] = z
            return self.reply(rid, z)
        if op == "theme":
            self.prefs["Dark"] = bool(m.get("dark"))
            self.desktop.set_dark(self.prefs["Dark"])
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
            return self.window.destroy()
        if op == "title":
            if self.pick:
                return self.reply(rid, True)
            t = str(m.get("text") or "")
            self.window.set_title(t + " - Atlas" if t else "Atlas")
            return self.reply(rid, True)
        if op == "hello":
            self.reply(rid, {"start": self.start_path, "native": True, "version": VERSION, "frameless": False,
                             "max": self.is_max, "linux": desktop.SYSTEM != "windows", "zoom": float(self.prefs.get("Zoom", 1.15)),
                             "isDefault": self.desktop.is_default()})
            self.page_ready = True
            if os.environ.get("ATLAS_DEBUG"):
                print("The page is up and talking to Atlas.", file=sys.stderr)
            for msg in self.pending_open:
                self.send(msg)
            self.pending_open = []
            if not self.pick and self.prefs.get("UpdateCheck", True) and time.time() - float(self.prefs.get("UpdateLast") or 0) > 86400:
                threading.Thread(target=self.update_check, args=(0,), daemon=True).start()
            return self.send_copies(True)
        if op == "win":
            act = str(m.get("action") or "")
            if act == "min":
                self.window.minimize()
            elif act == "max":
                self.window.restore() if self.is_max else self.window.maximize()
            elif act == "close":
                self.reply(rid, True)
                return self.window.destroy()
            return self.reply(rid, True)
        if op == "update":
            return self.update_request(rid, m)
        if self.desktop.handle(rid, op, m):
            return
        self.reply(rid, error="Unknown request: " + op)

    # ---- New versions, from GitHub (atlas_update.py) -------------------------------------------------------
    def current_version(self):
        return os.environ.get("ATLAS_PRETEND_VERSION") or VERSION   # an older version, to try updating

    def update_info(self):
        rel = update.latest(self.current_version())
        self.release = rel
        return {"current": self.current_version(), "version": rel["version"], "url": rel["url"], "notes": rel["notes"][:4000],
                "newer": update.version_tuple(rel["version"]) > update.version_tuple(self.current_version()),
                "canInstall": bool(update.asset_for(desktop.SYSTEM, rel) and update.target(desktop.SYSTEM)),
                "auto": bool(self.prefs.get("UpdateCheck", True))}

    def update_check(self, rid):
        """Asked for (rid), or the daily look when Atlas starts (0): a new version is offered once, unless skipped."""
        try:
            info = self.update_info()
        except Exception as e:
            if rid:
                self.reply(rid, error="Couldn't reach GitHub to look for a new version (" + (getattr(e, "reason", None) and str(e.reason) or str(e)) + ").")
            return
        self.prefs["UpdateLast"] = time.time()
        if rid:
            return self.reply(rid, info)
        if info["newer"] and info["version"] != self.prefs.get("UpdateSkip"):
            self.event({"ev": "update", "info": info})

    def update_request(self, rid, m):
        act = str(m.get("action") or "")
        if act == "check":
            threading.Thread(target=self.update_check, args=(rid,), daemon=True).start()
        elif act == "install":
            threading.Thread(target=self.update_install, args=(rid,), daemon=True).start()
        elif act == "skip":
            self.prefs["UpdateSkip"] = str(m.get("version") or "")
            self.reply(rid, True)
        elif act == "auto":
            self.prefs["UpdateCheck"] = bool(m.get("on"))
            self.reply(rid, True)
        else:
            self.reply(rid, error="Unknown update request: " + act)

    def update_install(self, rid):
        try:
            rel = getattr(self, "release", None) or update.latest(self.current_version())
            a, where = update.asset_for(desktop.SYSTEM, rel), update.target(desktop.SYSTEM)
            if not a or not where:
                return self.reply(rid, error="This Atlas can't update itself. Get the new one from " + rel["url"])
            last = [0.0]

            def progress(done, total):
                if time.monotonic() - last[0] > 0.2 or done == total:
                    last[0] = time.monotonic()
                    self.send('{"ev":"job","id":%d,"data":%s}' % (rid, to_json({"kind": "update", "done": done, "total": total})))
            f = update.download(a, os.path.join(desktop.CACHE_DIR, "update"), progress)
            update.install(desktop.SYSTEM, f, where)
        except Exception as e:
            if os.environ.get("ATLAS_DEBUG"):
                traceback.print_exc()
            return self.reply(rid, error="The update didn't go in: " + (str(e) or e.__class__.__name__))
        self.reply(rid, {"restarting": True})
        # the new Atlas starts once this one has closed
        self.desktop.call_main(self.window.destroy)

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
            self.send('{"ev":"copies","list":' + j + "}")

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
                self.send('{"ev":"changed","path":' + to_json(p) + "}")
            self.changed = keep
        return True

    def drain(self, j):
        q = j.state.events
        while not q.empty():
            self.send('{"ev":"job","id":%d,"data":%s}' % (j.id, to_json(q.get())))

    # ---- The window --------------------------------------------------------------------------------------
    def maximized(self, now):
        if now != self.is_max:
            self.is_max = now
            self.send('{"ev":"win","max":%s}' % ("true" if now else "false"))

    def open_folder(self, path, select=None, props=False):
        self.desktop.present()
        if path and os.path.exists(path) and not os.path.isdir(path) and select is None:
            path, select = os.path.dirname(os.path.abspath(path)), [os.path.basename(path)]
        if path:
            msg = to_json({"ev": "open", "path": os.path.abspath(path), "select": select or [], "props": props})
            if self.page_ready:
                self.send(msg)
            else:
                self.pending_open.append(msg)

    def on_closing(self):
        # a copy still going: ask first, as closing stops it
        if any(c.status in ("running", "waiting") for c in self.copies):
            if not self.desktop.confirm("A copy is still going.", "Closing Atlas stops it, and the file being copied is not kept.\n\nClose anyway?"):
                return False
        for c in self.copies:
            c.state.cancel, c.state.paused = True, False
        for j in self.jobs.values():
            j.state.cancel = True
        geo = self.desktop.geometry()
        if geo:
            w, h, maxed = geo
            if not maxed:
                self.prefs["W"], self.prefs["H"] = w, h
            self.prefs["Max"] = maxed
        # a picker leaves Atlas's saved window size and zoom as they were
        if not self.pick:
            try:
                write_file(self.prefs_path, json.dumps(self.prefs))
            except OSError:
                pass
        self.desktop.stop()
        # give a cancelled copy a moment to remove its part-copied file
        until = time.monotonic() + 2
        while time.monotonic() < until and any(c.status == "running" and not c.job.done for c in self.copies):
            time.sleep(0.1)
        return True

    # ---- Thumbnails: made in the background and kept in ~/.cache/atlas/thumbs ---------------------------
    thumb_gate = threading.BoundedSemaphore(3)

    def thumb(self, p, size):
        st = os.stat(p)
        key = hashlib.sha1(("%s|%d|%d|%d" % (p, st.st_mtime_ns, st.st_size, size)).encode()).hexdigest()
        out = os.path.join(THUMB_DIR, key + ".png")
        if not os.path.exists(out):
            os.makedirs(THUMB_DIR, exist_ok=True)
            tmp = out + ".part"
            ext = os.path.splitext(p)[1].lower()
            with self.thumb_gate:
                self.desktop.make_thumb(p, ext, size, tmp, ext in VIDEO)
            if os.path.exists(tmp) and os.path.getsize(tmp):
                os.replace(tmp, out)
        if not os.path.exists(out):
            return None
        with open(out, "rb") as f:
            return f.read()


# ---- The page, and the files for the preview pane, file-type icons and thumbnails ----------------------
# The page asks for <base>file?p=<path> (a picture, a PDF, a video...), <base>icon?e=.docx and
# <base>thumb?p=<path>&s=256. Anything not starting with the secret, or not addressed to 127.0.0.1, is refused.
class Resources(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host):
        self.host = host
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def do_GET(self):
        host = self.server.host
        u = urllib.parse.urlsplit(self.path)
        prefix = "/" + host.token + "/"
        if self.headers.get("Host") != "127.0.0.1:%d" % self.server.server_address[1] or not u.path.startswith(prefix):
            return self.send_error(404)
        what, q = u.path[len(prefix):], urllib.parse.parse_qs(u.query)
        try:
            if what == "atlas.html":
                return self.send_data(self.page(host), "text/html; charset=utf-8")
            if what == "icon":
                # ?p= a file's own icon (a program's, on Windows), ?e= the one for its kind
                own = bool(q.get("p"))
                png = host.desktop.icon_png((q.get("p") if own else q.get("e") or [""])[0], (q.get("s") or [""])[0] == "l", own)
                return self.send_data(png, "image/png", cache=True) if png else self.send_error(404)
            if what == "thumb":
                size = max(64, min(1600, int((q.get("s") or ["256"])[0] or 256)))
                try:
                    png = host.thumb((q.get("p") or [""])[0], size)
                except Exception:  # a thumbnail that can't be made just isn't shown
                    png = None
                return self.send_data(png, "image/png", cache=True) if png else self.send_error(404)
            if what == "file":
                p = (q.get("p") or [""])[0]
                if not p or core.kind(p) != 1:
                    return self.send_error(404)
                return self.send_file(p)
        except (BrokenPipeError, ConnectionResetError):
            return
        except OSError:
            return self.send_error(404)
        self.send_error(404)

    def page(self, host):
        with open(PAGE, "rb") as f:
            data = f.read()
        # the page's paths are /-separated everywhere but Windows (its "Linux" mode is macOS's too)
        js = BRIDGE % {"linux": "true" if desktop.SYSTEM != "windows" else "false", "system": to_json(desktop.SYSTEM), "res": to_json(host.base),
                       "saved": to_json(host.saved).replace("</", "<\\/")}
        tag = b"<head>"
        i = data.find(tag)
        i = i + len(tag) if i >= 0 else 0
        return data[:i] + b"<script>" + js.encode() + b"</script>" + data[i:]

    def send_data(self, data, ctype, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(data)

    # a file, or the part of it asked for (a video being skipped through)
    def send_file(self, p):
        size = os.path.getsize(p)
        start, end, status = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if m and size and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        with open(p, "rb") as f:
            f.seek(start)
            self.send_response(status)
            self.send_header("Content-Type", TYPES.get(os.path.splitext(p)[1].lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(262144, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def main():
    pick = None
    args = sys.argv[1:]
    if len(args) > 1 and args[0] == "--pick":
        try:
            pick = json.loads(args[1])
        except ValueError:
            sys.exit("--pick needs JSON, like '{\"mode\": \"open\"}'")
        args = []
    # --check-update: asks GitHub for the newest release and says what it found (for trying the built app)
    if args[:1] == ["--check-update"]:
        try:
            rel = update.latest(VERSION)
            print("Atlas %s: the newest release is %s, with %s" % (VERSION, rel["version"], ", ".join(sorted(rel["assets"]))), file=sys.stderr)
        except Exception as e:
            print("Atlas %s couldn't reach GitHub: %s" % (VERSION, e), file=sys.stderr)
            sys.exit(1)
        return
    path = desktop.start_path(args[0], os.getcwd()) if args else ""
    # Atlas runs once per person: opened again (from the launcher, or with a folder), it passes the folder
    # to the window already open, which comes to the front and opens the folder in a new tab.
    # A picker is a window of its own, apart from the Atlas that may be open.
    if not pick and desktop.hand_over(path):
        return
    # the window's app id, as Hyprland and the launcher see it (a picker has its own, so it can float)
    if desktop.SYSTEM == "linux":
        from gi.repository import GLib
        GLib.set_prgname(desktop.APP_ID + ".Picker" if pick else desktop.APP_ID)
        GLib.set_application_name("Atlas")
    Host(path, pick).run()


if __name__ == "__main__":
    main()
