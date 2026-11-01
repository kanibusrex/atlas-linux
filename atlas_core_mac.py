"""
atlas_core_mac.py: the file work that is macOS's own: the startup disk and what's in /Volumes (other disks,
USB sticks, disk images, network shares), the Trash (through Finder's own NSFileManager), your Movies
folder, hidden files, a server's shares (smbutil), what's using a file (lsof) and the desktop picture.
atlas_core.py takes from here what it can't do the same way everywhere; atlas_core_linux.py and
atlas_core_windows.py give the same names.

Like atlas_core.py, no windows here, only files: everything can run in a background thread.
"""

import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import threading
import time

import atlas_core as core

PATH_HINT = "~/Documents or /Volumes"
ELSEWHERE = "Windows"   # where most of the requests in NOT_HERE work


# ---- Your own folders ----------------------------------------------------------------------------
NAMES = {"DESKTOP": "Desktop", "DOCUMENTS": "Documents", "DOWNLOAD": "Downloads", "PICTURES": "Pictures",
         "MUSIC": "Music", "VIDEOS": "Movies", "TEMPLATES": "Templates"}


def user_dir(key):
    return os.path.join(os.path.expanduser("~"), NAMES.get(key, key.title()))


# ---- Drives: the startup disk, and everything in /Volumes ------------------------------------------
_NET_FS = {"smbfs", "afpfs", "nfs", "webdav", "ftp", "cifs"}
_MOUNT_LINE = re.compile(r"^(.*) on (/.*) \((\w+)")


def _mounts():
    """{mount point: (what is mounted, file system)}, from `mount`."""
    out = {}
    try:
        r = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return out
    for ln in r.stdout.splitlines():
        m = _MOUNT_LINE.match(ln)
        if m:
            out[m.group(2)] = (m.group(1), m.group(3))
    return out


_NET_FS = {"smbfs", "afpfs", "nfs", "webdav", "ftp", "osxfuse", "macfuse"}


def slow_target(path):
    """Is this folder on a network share? atlas_core.copy paces copies there."""
    path = os.path.realpath(path)
    best, fs = "", ""
    for mnt, (_what, f) in _mounts().items():
        if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
            best, fs = mnt, f
    return fs in _NET_FS


def _statvfs(path, wait=2.0):
    """statvfs on a share asks the server; one that has gone away must not hold up the drive list."""
    box = {}

    def go():
        try:
            box["v"] = os.statvfs(path)
        except OSError:
            pass
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(wait)
    return box.get("v")


def _startup_name():
    """The startup disk's name, as Finder shows it ("Macintosh HD")."""
    try:
        for n in os.listdir("/Volumes"):
            p = os.path.join("/Volumes", n)
            if os.path.islink(p) and os.path.realpath(p) == "/":
                return n
    except OSError:
        pass
    return "Macintosh HD"


def drives():
    mounts = _mounts()
    out = []
    spots = [("/", _startup_name())]
    try:
        for n in sorted(os.listdir("/Volumes")):
            p = os.path.join("/Volumes", n)
            if not n.startswith(".") and not os.path.islink(p) and os.path.isdir(p):
                spots.append((p, n))
    except OSError:
        pass
    for path, label in spots:
        src, fs = mounts.get(path, ("", ""))
        net = fs in _NET_FS
        info = {"path": path, "type": "Network" if net else ("Fixed" if path == "/" else "Removable"), "ready": False,
                "label": label, "total": 0, "free": 0, "unc": "", "fs": fs}
        if net and src.startswith("//"):
            info["unc"] = ("afp:" if fs == "afpfs" else "smb:") + re.sub(r":[^@/]*@", "@", src)   # never a password
        sv = _statvfs(path)
        if sv:
            info["total"] = sv.f_blocks * sv.f_frsize
            info["free"] = sv.f_bavail * sv.f_frsize
            info["ready"] = True
        out.append(info)
    home = os.path.expanduser("~")
    places = []
    for name, p in (("Home", home), ("Desktop", user_dir("DESKTOP")), ("Documents", user_dir("DOCUMENTS")),
                    ("Downloads", user_dir("DOWNLOAD")), ("Pictures", user_dir("PICTURES")), ("Movies", user_dir("VIDEOS")),
                    ("Applications", "/Applications")):
        if os.path.isdir(p):
            places.append({"name": name, "path": p})
    # Atlas doesn't show the Trash as a folder on macOS: Finder keeps where each thing came from to itself
    return {"drives": out, "clouds": cloud_places(), "places": places, "trash": {"path": "", "count": 0}, "user": pwd.getpwuid(os.getuid()).pw_name,
            "machine": os.uname().nodename.split(".")[0]}


# ---- A server's shares (smbutil, as a guest) --------------------------------------------------------
def smb_shares(server, user, domain, password):
    server = server.strip().strip("/\\")
    if not server or "/" in server or "\\" in server:
        raise RuntimeError("Type a server name, such as nas or 192.168.0.10.")
    if user:
        raise RuntimeError("Type the share as well, such as //server/share: macOS can only list a server's shares for a guest.")
    try:
        r = subprocess.run(["/usr/bin/smbutil", "view", "-g", "//" + server], capture_output=True, text=True, timeout=20,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError(server + " did not answer.")
    shares = []
    for ln in r.stdout.splitlines():
        m = re.match(r"^(.+?)\s+Disk\s*(.*)$", ln)
        if m and not m.group(1).endswith("$"):
            shares.append({"name": m.group(1).strip(), "comment": m.group(2).strip()})
    if not shares and r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(msg[-1] if msg else "Couldn't reach " + server + ".")
    return {"server": server, "shares": shares}


# ---- Deleting: to the Trash, the way Finder does --------------------------------------------------
_put_back = {}   # where things this Atlas moved to the Trash came from, so Undo can put them back


def trash(p):
    """Moves p to the Trash; returns where it went."""
    from Foundation import NSFileManager, NSURL
    ok, where, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(NSURL.fileURLWithPath_(p), None, None)
    if not ok:
        raise OSError(err.localizedDescription() if err else "Finder said no")
    return where.path() if where else ""


def remove_items(paths, permanent, state):
    removed, problems, trashed = 0, [], []
    for p in paths or []:
        if state and state.cancel:
            break
        p = str(p)
        try:
            if not core.exists(p):
                continue
            if permanent:
                core.remove_tree(p)
            else:
                where = trash(p)
                if where:
                    _put_back[where] = p
                    trashed.append({"from": p, "to": where})
            removed += 1
        except OSError as e:
            problems.append({"path": p, "error": e.strerror or str(e)})
    return {"removed": removed, "problems": problems, "trashed": trashed}


def trash_restore(paths):
    """Puts back what this Atlas moved to the Trash (Undo); anything else is put back from Finder."""
    restored, problems = [], []
    for p in paths or []:
        p = str(p)
        orig = _put_back.get(p)
        if not orig or not core.exists(p):
            problems.append({"path": p, "error": "Put it back from the Trash in Finder."})
            continue
        d = os.path.dirname(orig)
        try:
            os.makedirs(d, exist_ok=True)
            dest = core.join(d, core.free_name(d, os.path.basename(orig)))
            shutil.move(p, dest)
        except OSError as e:
            problems.append({"path": p, "error": e.strerror or str(e)})
            continue
        _put_back.pop(p, None)
        restored.append({"from": p, "to": dest})
    return {"restored": restored, "problems": problems}


# ---- What's using a file, folder or drive (lsof) ------------------------------------------------------
def in_use(paths):
    """Programs with something open in what was asked about. Other people's can't be seen without admin rights."""
    found = {}
    for p in paths or []:
        p = str(p)
        # a folder: everything open inside it; a file, or a drive (its top): that, or anything on it
        what = ["+D", p] if os.path.isdir(p) and not os.path.ismount(p) else ["--", p]
        try:
            r = subprocess.run(["/usr/sbin/lsof", "-F", "pcL", "-w"] + what, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        cur = None
        for ln in r.stdout.splitlines():
            k, v = ln[:1], ln[1:]
            if k == "p":
                cur = found.setdefault(int(v), {"pid": int(v), "name": "", "exe": "", "owner": "", "how": "has a file open"})
            elif cur is not None and k == "c":
                cur["name"] = v
            elif cur is not None and k == "L":
                cur["owner"] = v
    me = os.getpid()
    found.pop(me, None)
    return {"programs": sorted(found.values(), key=lambda x: x["name"].lower()), "checked": 0, "hidden": 0}


def end_program(pid):
    pid = int(pid)
    if pid <= 1 or pid == os.getpid():
        raise RuntimeError("Atlas won't end that one.")
    os.kill(pid, signal.SIGTERM)
    return True


# ---- The desktop picture --------------------------------------------------------------------------------
def set_wallpaper(path):
    script = 'tell application "System Events" to tell every desktop to set picture to (POSIX file "%s")' % \
        str(path).replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(["/usr/bin/osascript", "-"], input=script, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "macOS couldn't set it as the desktop picture.")
    return True


# ---- What atlas_core.py asks of every system --------------------------------------------------------
# ---- Cloud folders: the ones the services' own apps make ----------------------------------------------------
def cloud_places():
    home = os.path.expanduser("~")
    out = []
    icloud = os.path.join(home, "Library", "Mobile Documents", "com~apple~CloudDocs")
    try:
        if os.path.isdir(icloud):
            out.append({"path": icloud, "label": "iCloud Drive", "service": "iclouddrive", "own": False, "mounted": True, "ready": True, "total": 0, "free": 0})
    except OSError:
        pass
    top = os.path.join(home, "Library", "CloudStorage")
    try:
        names = sorted(os.listdir(top))
    except OSError:
        names = []
    for n in names:
        path = os.path.join(top, n)
        if n.startswith(".") or not os.path.isdir(path):
            continue
        maker, _, who = n.partition("-")
        service, label = {"OneDrive": ("onedrive", "OneDrive"), "GoogleDrive": ("drive", "Google Drive"), "Dropbox": ("dropbox", "Dropbox"), "Box": ("box", "Box")}.get(maker, ("", maker))
        if service == "drive" and os.path.isdir(os.path.join(path, "My Drive")):
            path = os.path.join(path, "My Drive")
        # OneDrive-Personal is just OneDrive; OneDrive-Contoso says whose
        if who and who not in ("Personal", "Box") and service != "drive":
            label += " - " + who
        out.append({"path": path, "label": label, "service": service, "own": False, "mounted": True, "ready": True, "total": 0, "free": 0, "who": who if service == "drive" else ""})
    return out


def online_only(_path, st):
    # SF_DATALESS: an iCloud, OneDrive or Google file whose contents are still in the cloud
    return bool(getattr(st, "st_flags", 0) & 0x40000000)


def is_hidden(name, st):
    return name.startswith(".") or bool(getattr(st, "st_flags", 0) & stat.UF_HIDDEN)


def hidden_entry(de):
    return de.name.startswith(".")


def owner(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def bad_name(_n):
    return ""


def listing(full, out):
    """A folder's listing as the page is given it: apps (Safari.app) are one thing to open, as in Finder,
    not folders to go into, with the size of everything in them."""
    apps = [e for e in out.get("entries", []) if e["d"] and e["n"].lower().endswith(".app")]
    for e in apps:
        e["d"] = False
    sizes = app_sizes([e.get("p") or core.join(full, e["n"]) for e in apps])
    for e in apps:
        e["s"] = sizes.get(e.get("p") or core.join(full, e["n"]), 0)
    return out


# ---- How big an app is: everything in its bundle, as Finder counts it ------------------------------
_counted = {}   # app -> (its modified time, bytes), for apps counted file by file


def _count(app):
    """Adds up every file in an app, links not followed (a framework's links point inside it)."""
    total, stack = 0, [app]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for de in it:
                    try:
                        if de.is_dir(follow_symlinks=False):
                            stack.append(de.path)
                        else:
                            total += de.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def _stamp(app):
    try:
        return os.stat(app).st_mtime_ns
    except OSError:
        return 0


def spotlight_sizes(apps):
    """What Spotlight knows of apps' sizes, in one ask: {app: bytes}, for those it has indexed."""
    if not apps:
        return {}
    try:
        r = subprocess.run(["/usr/bin/mdls", "-name", "kMDItemLogicalSize", "-raw", "-nullMarker", ""] + apps,
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    vals = r.stdout.decode("utf-8", "replace").split("\0")
    return {a: int(v) for a, v in zip(apps, vals) if v.strip().isdigit() and int(v) > 0}


def app_sizes(apps, budget=1.5):
    """{app: bytes} for apps: Spotlight's figure, or (for apps it doesn't know) counted, as long as the
    counting takes, in all, no more than budget seconds; the rest are counted in the background, ready for
    next time."""
    out = spotlight_sizes(apps)
    left = []
    for a in apps:
        if a in out:
            continue
        was = _counted.get(a)
        if was and was[0] == _stamp(a):
            out[a] = was[1]
        else:
            left.append(a)
    until = time.monotonic() + budget
    while left and time.monotonic() < until:
        a = left.pop(0)
        _counted[a] = (_stamp(a), _count(a))
        out[a] = _counted[a][1]
    if left:
        def later(rest=left):
            for a in rest:
                _counted[a] = (_stamp(a), _count(a))
        threading.Thread(target=later, daemon=True).start()
    return out


def details(path):
    r = core.details(path)
    if r.get("d") and path.rstrip("/").lower().endswith(".app"):
        r["d"] = False
        r["s"] = app_sizes([path], budget=60).get(path, 0)
    return r


# requests answered here, each given the page's arguments and the request's State
OPS = {
    "drives": lambda a, state: drives(),
    "details": lambda a, state: details(str(a.get("path"))),
    "delete": lambda a, state: remove_items(a.get("paths"), bool(a.get("permanent")), state),
    "recyclable": lambda a, state: {"ok": True},
    "restore": lambda a, state: trash_restore(a.get("paths")),
    "smbshares": lambda a, state: smb_shares(str(a.get("server") or ""), str(a.get("user") or ""), str(a.get("domain") or ""), str(a.get("password") or "")),
    "inuse": lambda a, state: in_use(a.get("paths")),
    "endprogram": lambda a, state: end_program(a.get("pid")),
    "wallpaper": lambda a, state: set_wallpaper(str(a.get("path") or "")),
}

# the Windows Atlas's own (mapped drives, a PC's shares, Active Directory), and emptying the Trash (Finder's)
NOT_HERE = {"shares", "access", "members", "letters", "mapdrive", "unmapdrive", "emptytrash"}
