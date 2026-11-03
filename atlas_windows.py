"""
Atlas on Windows: what atlas.py asks of the desktop here. pywebview's window is WinForms with WebView2,
reached through pythonnet, so the clipboard, the Save dialog and the icons are the same .NET calls
AtlasShell.ps1 made. Requests from the page are worked through one at a time on a thread of Atlas's
own (call_main, from atlas_worker.py); anything that must touch the window or the clipboard is handed to the window's
thread (ui).

The same few names (Desktop, DATA_DIR, CACHE_DIR, SYSTEM) are what atlas_linux.py gives on Linux.
"""

import base64
import ctypes
import os
import re
import secrets
import shutil
import subprocess
import threading
from ctypes import wintypes
from multiprocessing.connection import Client, Listener

import atlas_core as core
from atlas_worker import Worker

SYSTEM = "windows"
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local"), "Atlas")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
# a second Atlas hands its folder to the one already open, through this pipe (one per person)
PIPE = r"\\.\pipe\atlas-" + re.sub(r"[^A-Za-z0-9_.-]", "_", os.environ.get("USERNAME", "me"))

user32 = ctypes.WinDLL("user32")
shell32 = ctypes.WinDLL("shell32")
shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
shell32.SHGetFileInfoW.restype = ctypes.c_void_p
shell32.SHObjectProperties.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.DestroyIcon.argtypes = [wintypes.HICON]
shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = ctypes.c_void_p


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int), ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", wintypes.WCHAR * 260), ("szTypeName", wintypes.WCHAR * 80)]


def _pipe_key():
    """The secret both ends of the pipe know: kept in Atlas's folder, which only you can read."""
    os.makedirs(DATA_DIR, exist_ok=True)
    p = os.path.join(DATA_DIR, "pipe.key")
    try:
        with open(p, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_bytes(32)
    with open(p, "wb") as f:
        f.write(key)
    return key


def hand_over(path):
    """Passes the folder to an Atlas that is already running. False when none is."""
    try:
        c = Client(PIPE, family="AF_PIPE", authkey=_pipe_key())
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
    """A folder (or file) from the command line, as a full path."""
    arg = arg.strip('"')
    return os.path.normpath(os.path.join(cwd, os.path.expandvars(os.path.expanduser(arg)))) if arg else ""


class Desktop(Worker):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.window = None
        self.watchers = {}
        self.letters = None
        self.icon_cache = {}

    # ---- The window's own thread -------------------------------------------------------------------
    def form(self):
        from webview.platforms.winforms import BrowserView
        return BrowserView.instances.get(self.window.uid) if self.window else None

    def ui(self, fn):
        """Runs fn on the window's thread and returns what it gave."""
        f = self.form()
        if f is None:
            return None
        from System import Func, Type
        out = {}

        def run():
            out["v"] = fn(f)
        f.Invoke(Func[Type](run))
        return out.get("v")

    def ui_later(self, fn):
        """Runs fn on the window's thread without waiting for it (for something that takes as long as you do)."""
        f = self.form()
        if f is None:
            return False
        from System import Action
        f.BeginInvoke(Action(lambda: fn(f)))
        return True

    # ---- Starting up: before the window, and once it is made ------------------------------------------
    def start(self):
        self.start_worker()
        if not self.host.pick:
            threading.Thread(target=self.listen, daemon=True).start()
        self.every(2000, self.check_drives)

    def attach(self, window):
        self.window = window
        window.events.maximized += lambda: self.call_main(self.host.maximized, True)
        window.events.restored += lambda: self.call_main(self.host.maximized, False)
        window.events.loaded += self.on_loaded

    def on_loaded(self):
        self.set_zoom(float(self.host.prefs.get("Zoom", 1.15)))
        self.set_dark(bool(self.host.prefs.get("Dark", True)))

    def listen(self):
        try:
            ln = Listener(PIPE, family="AF_PIPE", authkey=_pipe_key())
        except OSError:
            return
        while True:
            try:
                c = ln.accept()
            except Exception:  # someone without the key, or a broken connection
                continue
            try:
                path = c.recv()
                self.call_main(self.host.open_folder, str(path or ""))
            except Exception:
                pass
            finally:
                c.close()

    # ---- The window --------------------------------------------------------------------------------------
    def geometry(self):
        """The window's size (when not maximized, in the same units it was made with), and whether it is maximized."""
        def get(f):
            import System.Windows.Forms as WinForms
            maxed = f.WindowState == WinForms.FormWindowState.Maximized
            b = f.RestoreBounds if maxed else f.Bounds
            s = f._scale or 1.0
            return int(b.Width / s), int(b.Height / s), maxed
        try:
            return self.ui(get)
        except Exception:
            return None

    def present(self):
        def go(f):
            import System.Windows.Forms as WinForms
            if f.WindowState == WinForms.FormWindowState.Minimized:
                f.WindowState = WinForms.FormWindowState.Normal
            f.Activate()
            user32.SetForegroundWindow(f.Handle.ToInt64())
        try:
            self.ui(go)
        except Exception:
            pass

    def set_zoom(self, z):
        def go(f):
            f.webview.ZoomFactor = z
        try:
            self.ui(go)
        except Exception:
            pass

    def set_dark(self, dark):
        # a dark title bar to go with a dark theme (Windows 10 2004 and later; 19 before that)
        def go(f):
            v = ctypes.c_int(1 if dark else 0)
            dwm = ctypes.WinDLL("dwmapi")
            hwnd = wintypes.HWND(f.Handle.ToInt64())
            if dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), 4) != 0:
                dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(v), 4)
        try:
            self.ui(go)
        except Exception:
            pass

    def confirm(self, text, more):
        import System.Windows.Forms as WinForms
        ans = WinForms.MessageBox.Show(text + "\n\n" + more, "Atlas", WinForms.MessageBoxButtons.YesNo, WinForms.MessageBoxIcon.Question)
        return ans == WinForms.DialogResult.Yes

    def stop(self):
        for w in list(self.watchers.values()):
            w.EnableRaisingEvents = False
            w.Dispose()
        self.watchers = {}

    def is_default(self):
        return False

    def check_drives(self):
        # a USB stick in or out, a drive mapped: the page redraws its drives
        now = core.system.drive_letters()
        if self.letters is not None and now != self.letters:
            self.host.event({"ev": "drives"})
        self.letters = now
        return True

    # ---- What the page asks of the desktop ---------------------------------------------------------------
    def handle(self, rid, op, m):
        """Answers a request the desktop deals with. False when it isn't one of them."""
        reply = self.host.reply
        if op == "open":
            for p in m.get("paths") or []:
                os.startfile(str(p))
            reply(rid, True)
        elif op == "openwith":
            subprocess.Popen("rundll32.exe shell32.dll,OpenAs_RunDLL " + str(m.get("path")))
            reply(rid, True)
        elif op == "reveal":
            subprocess.Popen('explorer.exe /select,"' + str(m.get("path")) + '"')
            reply(rid, True)
        elif op == "explorer":
            subprocess.Popen('explorer.exe "' + str(m.get("path")) + '"')
            reply(rid, True)
        elif op == "terminal":
            path = str(m.get("path"))
            wt = shutil.which("wt.exe")
            if wt:
                subprocess.Popen([wt, "-d", path])
            else:
                subprocess.Popen(["powershell.exe", "-NoExit", "-NoProfile"], cwd=path, creationflags=subprocess.CREATE_NEW_CONSOLE)
            reply(rid, True)
        elif op == "runps":
            self.run_powershell(str(m.get("path") or ""), bool(m.get("admin")))
            reply(rid, True)
        elif op == "dragout":
            paths = [str(p) for p in m.get("paths") or [] if os.path.exists(str(p))]
            if not paths or not self.ui_later(lambda f: self.drag_out(f, rid, paths)):
                reply(rid, None)
        elif op == "properties":
            # Windows' own Properties window
            p = str(m.get("path"))
            self.ui(lambda f: shell32.SHObjectProperties(wintypes.HWND(f.Handle.ToInt64()), 2, p, None))
            reply(rid, True)
        elif op == "clipset":
            self.ui(lambda f: self.clip_set([str(p) for p in m.get("paths") or []], bool(m.get("cut"))))
            reply(rid, True)
        elif op == "clipget":
            reply(rid, self.ui(lambda f: self.clip_get()))
        elif op == "copytext":
            import System.Windows.Forms as WinForms
            text = str(m.get("text") or "")
            self.ui(lambda f: WinForms.Clipboard.SetText(text) if text else WinForms.Clipboard.Clear())
            reply(rid, True)
        elif op == "savetext":
            reply(rid, self.ui(lambda f: self.save_text(f, str(m.get("name") or "export.csv"), str(m.get("text") or ""))))
        elif op == "url":
            u = str(m.get("url") or "")
            if u.startswith("https://"):
                os.startfile(u)
            reply(rid, True)
        else:
            return False
        return True

    # "Run with PowerShell", as Explorer does it: in its own window, in the script's folder, past the
    # execution policy unless only signed scripts may run; and the same as administrator
    def run_powershell(self, path, admin):
        if not path.lower().endswith(".ps1") or not os.path.isfile(path):
            raise RuntimeError("That isn't a PowerShell script.")
        folder = os.path.dirname(path)
        q = lambda s: "'" + s.replace("'", "''") + "'"
        script = ("Set-Location -LiteralPath " + q(folder) + "\n"
                  "if ((Get-ExecutionPolicy) -ne 'AllSigned') { Set-ExecutionPolicy -Scope Process Bypass -Force }\n"
                  "& " + q(path) + "\n")
        args = "-EncodedCommand " + base64.b64encode(script.encode("utf-16-le")).decode()
        if admin:
            r = shell32.ShellExecuteW(None, "runas", "powershell.exe", args, folder, 1)
            if (r or 0) <= 32 and r != 5:   # 5: you said no to Windows' question, which is an answer
                raise RuntimeError("Windows couldn't start PowerShell as administrator.")
            return
        # Windows PowerShell finds its own modules: PowerShell 7's (when Atlas was started from it) won't load in it
        env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
        subprocess.Popen(["powershell.exe"] + args.split(" "), cwd=folder, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)

    # dragging files out of Atlas: a drag of Windows' own, with the files in it the way Explorer gives them,
    # so they can be dropped in Explorer, on the desktop, into an email or any other app (and back into Atlas)
    def drag_out(self, f, rid, paths):
        effect = None
        try:
            import System.Windows.Forms as WinForms
            from System.Collections.Specialized import StringCollection
            # the button already let go: no drag, or it would drop wherever the pointer is
            if WinForms.Control.MouseButtons == getattr(WinForms.MouseButtons, "None"):
                return
            sc = StringCollection()
            for p in paths:
                sc.Add(p)
            do = WinForms.DataObject()
            do.SetFileDropList(sc)
            do.SetText("\r\n".join(paths))
            E = WinForms.DragDropEffects
            effect = str(f.DoDragDrop(do, E.Copy | E.Move | E.Link))
        except Exception:
            pass
        finally:
            self.host.reply(rid, effect)

    # files on the clipboard the way Explorer puts them there, so a paste there (or here) works
    def clip_set(self, paths, cut):
        import System.Windows.Forms as WinForms
        from System import BitConverter, Int32
        from System.Collections.Specialized import StringCollection
        from System.IO import MemoryStream
        sc = StringCollection()
        for p in paths:
            sc.Add(p)
        do = WinForms.DataObject()
        do.SetFileDropList(sc)
        # how Explorer knows a cut from a copy: 2 is move, 5 is copy
        do.SetData("Preferred DropEffect", MemoryStream(BitConverter.GetBytes(Int32(2 if cut else 5))))
        do.SetText("\r\n".join(paths))
        WinForms.Clipboard.SetDataObject(do, True)

    def clip_get(self):
        import System.Windows.Forms as WinForms
        from System import Array, BitConverter, Byte
        if not WinForms.Clipboard.ContainsFileDropList():
            return {"paths": []}
        paths = [str(p) for p in WinForms.Clipboard.GetFileDropList()]
        cut = False
        try:
            eff = WinForms.Clipboard.GetDataObject().GetData("Preferred DropEffect")
            if eff is not None:
                b = Array.CreateInstance(Byte, 4)
                eff.Read(b, 0, 4)
                cut = (BitConverter.ToInt32(b, 0) & 2) != 0
        except Exception:
            pass
        return {"paths": paths, "cut": cut}

    def save_text(self, f, name, text):
        import System.Windows.Forms as WinForms
        dlg = WinForms.SaveFileDialog()
        dlg.FileName = name
        dlg.Filter = "CSV file (*.csv)|*.csv|All files (*.*)|*.*"
        dlg.InitialDirectory = core.user_dir("DOCUMENTS")
        dlg.OverwritePrompt = True
        if dlg.ShowDialog(f) != WinForms.DialogResult.OK:
            return {"cancelled": True}
        # with a byte-order mark, so Excel reads names with accents correctly
        with open(dlg.FileName, "w", encoding="utf-8-sig", newline="") as out:
            out.write(text)
        return {"path": str(dlg.FileName)}

    # ---- Watching open folders for changes ------------------------------------------------------------
    def watch(self, paths):
        from System.IO import FileSystemWatcher, NotifyFilters
        want = set(paths)
        for p in list(self.watchers):
            if p not in want:
                w = self.watchers.pop(p)
                w.EnableRaisingEvents = False
                w.Dispose()
        for p in want:
            if p in self.watchers or len(self.watchers) >= 16 or not os.path.isdir(p):
                continue
            try:
                w = FileSystemWatcher(p)
                w.IncludeSubdirectories = False
                w.NotifyFilter = NotifyFilters.FileName | NotifyFilters.DirectoryName | NotifyFilters.LastWrite | NotifyFilters.Size

                def changed(_s, _e, me=p):
                    self.call_main(self.host.changed.add, me)
                w.Created += changed
                w.Deleted += changed
                w.Changed += changed
                w.Renamed += changed
                w.EnableRaisingEvents = True
            except Exception:  # a folder Windows won't watch (some network drives) just isn't
                continue
            self.watchers[p] = w

    # ---- File-type icons, for the page (asked for by atlas.py's server) ------------------------------------
    def icon_png(self, what, large, own=False):
        """The icon Explorer shows: a file's own (a program's), or the one for a kind of file (.docx, or "folder")."""
        key = ("f:" if own else "e:") + ("L:" if large else "S:") + what.lower()
        if key in self.icon_cache:
            return self.icon_cache[key]
        ctypes.WinDLL("ole32").CoInitialize(None)          # the shell asks COM for some icons; once per thread is enough
        info = SHFILEINFOW()
        flags = 0x100 | (0 if large else 0x1)              # an icon, small or large
        attrs, ask = 0, what
        if not own:
            flags |= 0x10                                    # by type, without opening anything
            ask, attrs = ("folder", 0x10) if what == "folder" else ("file" + what, 0x80)
        if not shell32.SHGetFileInfoW(ask, attrs, ctypes.byref(info), ctypes.sizeof(info), flags) or not info.hIcon:
            return None
        png = None
        try:
            from System import IntPtr
            from System.Drawing import Icon
            from System.Drawing.Imaging import ImageFormat
            from System.IO import MemoryStream
            ic = Icon.FromHandle(IntPtr(info.hIcon))
            bm = ic.ToBitmap()
            ms = MemoryStream()
            bm.Save(ms, ImageFormat.Png)
            png = bytes(ms.ToArray())
            bm.Dispose()
            ic.Dispose()
        finally:
            user32.DestroyIcon(info.hIcon)
        if len(self.icon_cache) < 4000:
            self.icon_cache[key] = png
        return png

    def make_thumb(self, p, ext, size, tmp, video):
        raise RuntimeError("Atlas makes no thumbnails on Windows yet.")

