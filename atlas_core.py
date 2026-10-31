"""
atlas_core.py: Atlas's file operations, for Linux. A port of AtlasCore.ps1 with the same requests and
answers, so atlas.html works unchanged. No windows here, only files, so every function can run in a
background thread and be tested on its own.

Every function that can take a while is given a State holding:
  events   a queue of progress messages for the window to pass on to the page
  cancel   set to True by the window to stop the work
  paused   set to True by the window to pause a copy
"""

import fnmatch
import hashlib
import os
import pwd
import queue
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile

SEP = os.sep


class State:
    def __init__(self):
        self.events = queue.SimpleQueue()
        self.cancel = False
        self.paused = False
        self.bytes_done = 0
        self.bytes_total = 0
        self.files_done = 0
        self.files_total = 0
        self.current = ""
        self.phase = "waiting"


def send_event(state, message):
    if state is not None:
        state.events.put(message)


def epoch_ms(t):
    if not t or t < 31536000:
        return 0
    return int(t * 1000)


# ---- Basic helpers -----------------------------------------------------------------------------
def kind(p):
    """0 not there, 1 a file, 2 a folder"""
    try:
        st = os.stat(p)
    except OSError:
        return 1 if os.path.islink(p) else 0
    return 2 if stat.S_ISDIR(st.st_mode) else 1


def exists(p):
    return os.path.lexists(p)


def join(a, b):
    return a + b if a.endswith(SEP) else a + SEP + b


def parent_of(p):
    p = p.rstrip(SEP) or SEP
    return os.path.dirname(p)


def base_of(p):
    return os.path.basename(p.rstrip(SEP))


def is_dir_entry(de):
    try:
        return de.is_dir(follow_symlinks=True)
    except OSError:
        return False


def children(d):
    with os.scandir(d) as it:
        return list(it)


def to_entry(name, full, st_l=None):
    """One file or folder, compact: n name, d folder, s size, m modified, c created, a attributes
    (h hidden, r read-only, l link)"""
    try:
        if st_l is None:
            st_l = os.lstat(full)
        link = stat.S_ISLNK(st_l.st_mode)
        st = os.stat(full) if link else st_l
    except OSError:
        st = st_l
        link = True
    is_dir = bool(st and stat.S_ISDIR(st.st_mode))
    flags = ""
    if name.startswith("."):
        flags += "h"
    if not os.access(full, os.W_OK):
        flags += "r"
    if link:
        flags += "l"
    m = epoch_ms(st.st_mtime) if st else 0
    c = 0
    if st:
        c = epoch_ms(getattr(st, "st_birthtime", 0) or st.st_ctime)
    return {"n": name, "d": is_dir, "s": 0 if is_dir or not st else st.st_size, "m": m, "c": c, "a": flags}


def entry_of(de):
    try:
        st_l = de.stat(follow_symlinks=False)
    except OSError:
        st_l = None
    return to_entry(de.name, de.path, st_l)


# ---- A folder's contents ----------------------------------------------------------------------
_SHELL = {
    "desktop": "DESKTOP", "documents": "DOCUMENTS", "personal": "DOCUMENTS", "downloads": "DOWNLOAD",
    "pictures": "PICTURES", "mypictures": "PICTURES", "music": "MUSIC", "videos": "VIDEOS", "templates": "TEMPLATES",
}


def xdg_dir(key):
    home = os.path.expanduser("~")
    defaults = {"DESKTOP": "Desktop", "DOCUMENTS": "Documents", "DOWNLOAD": "Downloads", "PICTURES": "Pictures",
                "MUSIC": "Music", "VIDEOS": "Videos", "TEMPLATES": "Templates"}
    try:
        with open(os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), "user-dirs.dirs")) as f:
            for line in f:
                if line.startswith("XDG_" + key + "_DIR="):
                    v = line.split("=", 1)[1].strip().strip('"')
                    return v.replace("$HOME", home)
    except OSError:
        pass
    return os.path.join(home, defaults.get(key, key.title()))


def resolve_path(path):
    """A typed path: ~ for your own folder, $NAME and %NAME% environment names, shell: names"""
    p = str(path or "").strip().strip('"')
    if p.lower().startswith("shell:"):
        name, _, rest = p[6:].partition("/")
        key = name.lower().replace(" ", "")
        if key in _SHELL:
            p = xdg_dir(_SHELL[key]) + ("/" + rest if rest else "")
        elif key in ("profile", "home"):
            p = os.path.expanduser("~") + ("/" + rest if rest else "")
        elif key == "temp":
            p = tempfile.gettempdir()
    if "%" in p:
        p = _expand_percent(p)
    p = os.path.expanduser(os.path.expandvars(p))
    return p


def _expand_percent(p):
    out, i = "", 0
    while i < len(p):
        if p[i] == "%":
            j = p.find("%", i + 1)
            if j > i + 1 and p[i + 1:j] in os.environ:
                out += os.environ[p[i + 1:j]]
                i = j + 1
                continue
        out += p[i]
        i += 1
    return out


def list_dir(path):
    p = resolve_path(path)
    if not p.startswith(SEP):
        return {"path": p, "error": "Type a full path, such as ~/Documents or /tmp."}
    try:
        k = kind(p)
        if k == 1 and is_archive(p):
            return zip_list(p, "")
        if k != 2:
            z = find_zip_part(p)
            if z:
                return zip_list(z["zip"], z["inner"])
            return {"path": p, "error": "This folder could not be found. It may have been moved or deleted, or the drive is not mounted."}
        full = os.path.normpath(p)
        entries = []
        for de in children(full):
            try:
                entries.append(entry_of(de))
            except Exception:
                pass
        par = "" if full == SEP else os.path.dirname(full)
        if full == trash_files():
            return list_trash({"path": full, "parent": par, "entries": entries, "long": False})
        return {"path": full, "parent": par, "entries": entries, "long": False}
    except PermissionError:
        return {"path": p, "error": "You do not have permission to open this folder."}
    except OSError as e:
        return {"path": p, "error": e.strerror or str(e)}


# ---- Drives and the usual places ----------------------------------------------------------------
_NET_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "sshfs", "fuse.sshfs", "fuse.rclone", "davfs", "fuse.davfs2", "9p"}
_REAL_FS = {"ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "zfs", "iso9660", "udf", "jfs", "reiserfs", "bcachefs"}


def _unescape_mount(s):
    return s.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def drives():
    out, seen_dev = [], set()
    home = os.path.expanduser("~")
    try:
        with open("/proc/self/mounts") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    mounts = []
    for ln in lines:
        parts = ln.split()
        if len(parts) < 3:
            continue
        dev, mnt, fs = parts[0], _unescape_mount(parts[1]), parts[2]
        net = fs in _NET_FS
        if not net and fs not in _REAL_FS:
            continue
        if mnt.startswith(("/boot", "/efi", "/snap", "/var/lib/docker", "/proc", "/sys")) and mnt != "/":
            continue
        removable = mnt.startswith(("/run/media/", "/media/", "/mnt/"))
        # btrfs subvolumes of one disk show many times: keep the first mount of each device,
        # plus /home when it has its own subvolume on the same disk
        if not net and dev in seen_dev and mnt not in (home, "/home"):
            continue
        seen_dev.add(dev)
        mounts.append((dev, mnt, fs, net, removable))
    if not any(m[1] == "/" for m in mounts):
        mounts.insert(0, ("", "/", "", False, False))
    for dev, mnt, fs, net, removable in mounts:
        info = {"path": mnt, "type": "Network" if net else ("Removable" if removable else "Fixed"),
                "ready": False, "label": "", "total": 0, "free": 0, "unc": dev if net else "", "fs": fs}
        if mnt == "/":
            info["label"] = "File system"
        elif mnt in ("/home", home):
            info["label"] = "Home"
        else:
            info["label"] = os.path.basename(mnt) or mnt
        try:
            sv = os.statvfs(mnt)
            info["total"] = sv.f_blocks * sv.f_frsize
            info["free"] = sv.f_bavail * sv.f_frsize
            info["ready"] = True
        except OSError:
            pass
        # one disk, two mounts with the same numbers (/ and /home on btrfs): keep both, they are
        # both useful places to start from
        out.append(info)
    out.extend(gvfs_shares())
    places = []
    for name, p in (("Home", home), ("Documents", xdg_dir("DOCUMENTS")), ("Downloads", xdg_dir("DOWNLOAD")),
                    ("Pictures", xdg_dir("PICTURES")), ("Videos", xdg_dir("VIDEOS"))):
        if p and os.path.isdir(p) and not any(x["path"] == p for x in places):
            places.append({"name": name, "path": p})
    try:
        os.makedirs(trash_files(), exist_ok=True)
        os.makedirs(os.path.join(trash_dir(), "info"), exist_ok=True)
    except OSError:
        pass
    return {"drives": out, "places": places, "trash": {"path": trash_files(), "count": trash_count()}, "user": pwd.getpwuid(os.getuid()).pw_name, "machine": os.uname().nodename}


# ---- Network shares (SMB, through GVFS, as you: no root, no fstab) ---------------------------------
# A share connected in Atlas, the file manager or `gio mount` is a folder under /run/user/<uid>/gvfs,
# named like smb-share:server=nas,share=media,user=bob. /proc/mounts only shows the one gvfsd-fuse
# mount, so they are found here instead.
def gvfs_root():
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid(), "gvfs")


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


def gvfs_shares():
    """Network places connected through GVFS: SMB shares, and SFTP, FTP and WebDAV servers."""
    out = []
    root = gvfs_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for nm in names:
        kind_, _, spec = nm.partition(":")
        kv = {}
        for part in spec.split(","):
            k, _, v = part.partition("=")
            kv[k] = urllib.parse.unquote(v)
        user = kv["user"] + "@" if kv.get("user") else ""
        host = kv.get("host") or kv.get("server") or ""
        port = ":" + kv["port"] if kv.get("port") else ""
        if kind_ == "smb-share":
            share = kv.get("share", "")
            if not host or not share:
                continue
            label, unc, fs = share + " on " + host, "smb://" + user + host + "/" + share, "smb"
        elif kind_ in ("sftp", "ftp", "ftps") and host:
            label, unc, fs = host, kind_ + "://" + user + host + port + "/", kind_
        elif kind_ == "dav" and host:
            scheme = "davs" if kv.get("ssl") == "true" else "dav"
            prefix = kv.get("prefix", "/")
            label, unc, fs = host + (prefix.rstrip("/") if prefix not in ("", "/") else ""), scheme + "://" + user + host + port + prefix, scheme
        else:
            continue
        mnt = os.path.join(root, nm)
        info = {"path": mnt, "type": "Network", "ready": False, "label": label, "total": 0, "free": 0,
                "unc": unc, "fs": fs, "gvfs": True}
        sv = _statvfs(mnt)
        if sv:
            info["total"] = sv.f_blocks * sv.f_frsize
            info["free"] = sv.f_bavail * sv.f_frsize
            info["ready"] = True
        out.append(info)
    return out


def smb_shares(server, user, domain, password):
    """The shares a server offers (smbclient -L), for picking one when only a server was typed."""
    exe = shutil.which("smbclient")
    if not exe:
        raise RuntimeError("Listing a server's shares needs smbclient. Type the share as well, such as //server/share.")
    server = server.strip().strip("/\\")
    if not server or "/" in server or "\\" in server:
        raise RuntimeError("Type a server name, such as nas or 192.168.0.10.")
    cmd = [exe, "-L", "//" + server, "-g"]
    env = dict(os.environ)
    if user:
        cmd += ["-U", user]
        env["PASSWD"] = password or ""   # by the environment, so the password is never on a command line
        if domain:
            cmd += ["-W", domain]
    else:
        cmd.append("-N")
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError(server + " did not answer.")
    shares = []
    for ln in r.stdout.splitlines():
        parts = ln.split("|")
        if len(parts) >= 2 and parts[0] == "Disk" and not parts[1].endswith("$"):
            shares.append({"name": parts[1], "comment": parts[2] if len(parts) > 2 else ""})
    if not shares and r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        msg = msg[-1] if msg else "smbclient failed."
        if "NT_STATUS_LOGON_FAILURE" in msg or "NT_STATUS_ACCESS_DENIED" in msg:
            msg = "The server turned down that user name and password."
        elif "NT_STATUS_" in msg and ("UNSUCCESSFUL" in msg or "NOT_FOUND" in msg or "TIMEOUT" in msg or "UNREACHABLE" in msg or "REFUSED" in msg):
            msg = "Couldn't reach " + server + "."
        raise RuntimeError(msg)
    return {"server": server, "shares": shares}


# ---- Searching ----------------------------------------------------------------------------------
def search(root, query, options, state):
    options = options or {}
    mx = int(options.get("max") or 5000)
    q = str(query or "").strip()
    wild = "*" in q or "?" in q
    words = [] if wild else [w for w in q.lower().split() if w]
    exts = []
    if options.get("ext"):
        for x in str(options["ext"]).lower().replace(",", " ").replace(";", " ").split():
            exts.append(x if x.startswith(".") else "." + x)
    typ = str(options.get("type") or "")
    after = int(options.get("after") or 0)
    min_s = int(options["minSize"]) if options.get("minSize") not in (None, "") else -1
    max_s = int(options["maxSize"]) if options.get("maxSize") not in (None, "") else -1
    recurse = options.get("recurse") is not False
    hidden = options.get("hidden") is True
    qlow = q.lower()

    found = scanned = 0
    batch = []
    last_send = time.monotonic()
    stack = [root]
    d = root
    while stack:
        if state and state.cancel:
            break
        d = stack.pop()
        try:
            items = children(d)
        except OSError:
            continue
        for de in items:
            if state and state.cancel:
                break
            scanned += 1
            name = de.name
            if not hidden and name.startswith("."):
                continue
            isdir = is_dir_entry(de)
            # links to other folders are not followed, so a loop cannot trap the search
            if isdir and recurse and not de.is_symlink():
                stack.append(de.path)
            if wild:
                if not fnmatch.fnmatchcase(name.lower(), qlow):
                    continue
            elif words:
                low = name.lower()
                if not all(w in low for w in words):
                    continue
            if typ == "folder" and not isdir:
                continue
            if typ == "file" and isdir:
                continue
            if exts and (isdir or os.path.splitext(name)[1].lower() not in exts):
                continue
            e = entry_of(de)
            if after > 0 and e["m"] < after:
                continue
            if not isdir and min_s >= 0 and e["s"] < min_s:
                continue
            if not isdir and max_s >= 0 and e["s"] > max_s:
                continue
            e["p"] = de.path
            batch.append(e)
            found += 1
            if found >= mx:
                break
        now = time.monotonic()
        if len(batch) >= 200 or (batch and now - last_send > 0.25):
            send_event(state, {"kind": "search", "items": batch, "scanned": scanned, "dir": d})
            batch = []
            last_send = now
        elif now - last_send > 0.5:
            send_event(state, {"kind": "search", "items": [], "scanned": scanned, "dir": d})
            last_send = now
        if found >= mx:
            break
    if batch:
        send_event(state, {"kind": "search", "items": batch, "scanned": scanned, "dir": ""})
    return {"found": found, "scanned": scanned, "capped": found >= mx, "cancelled": bool(state and state.cancel)}


# ---- Folder sizes --------------------------------------------------------------------------------
def measure(path, state, live):
    nbytes = files = dirs = denied = 0
    stack = [path]
    last = time.monotonic()
    while stack:
        if state and state.cancel:
            break
        d = stack.pop()
        try:
            items = children(d)
        except OSError:
            denied += 1
            continue
        for de in items:
            try:
                if de.is_dir(follow_symlinks=False):
                    stack.append(de.path)
                    dirs += 1
                else:
                    nbytes += de.stat(follow_symlinks=False).st_size
                    files += 1
            except OSError:
                pass
        if live and time.monotonic() - last > 0.4:
            send_event(state, {"kind": "size", "path": path, "bytes": nbytes, "files": files, "dirs": dirs, "partial": True})
            last = time.monotonic()
    return {"path": path, "bytes": nbytes, "files": files, "dirs": dirs, "denied": denied, "partial": bool(state and state.cancel)}


def sizes(paths, state):
    out = []
    for p in paths or []:
        if state and state.cancel:
            break
        r = measure(str(p), state, True)
        r["kind"] = "size"
        send_event(state, r)
        out.append(r)
    return {"sizes": out}


def children_sizes(path, state):
    """the size of every folder inside one, for "What's taking up space" """
    l = list_dir(path)
    if l.get("error"):
        raise RuntimeError(l["error"])
    paths = [join(l["path"], e["n"]) for e in l["entries"] if e["d"]]
    files = sum(e["s"] for e in l["entries"] if not e["d"])
    send_event(state, {"kind": "files", "bytes": files, "path": path})
    r = sizes(paths, state)
    r["filesBytes"] = files
    return r


# ---- Names ---------------------------------------------------------------------------------------
def check_name(n):
    n = str(n or "")
    if not n.strip():
        return "A name cannot be empty."
    if "/" in n or "\0" in n:
        return "A name cannot contain a / character."
    if n in (".", ".."):
        return "A name cannot be . or .."
    if len(n.encode("utf-8")) > 255:
        return "That name is too long."
    return ""


def free_name(d, name):
    """A free name in a folder: "Report.docx", then "Report (2).docx", "Report (3).docx"..."""
    target = join(d, name)
    tk = kind(target)
    if tk == 0 and not exists(target):
        return name
    base, ext = os.path.splitext(name)
    if tk == 2:
        base, ext = name, ""
    import re
    m = re.match(r"^(.*) \((\d+)\)$", base)
    if m:
        base = m.group(1)
    for i in range(2, 10000):
        cand = "%s (%d)%s" % (base, i, ext)
        if not exists(join(d, cand)):
            return cand
    return "%s (%s)%s" % (base, uuid.uuid4().hex[:6], ext)


def new_folder(d, name):
    bad = check_name(name)
    if bad:
        return {"error": bad}
    n = free_name(d, name)
    full = join(d, n)
    os.makedirs(full, exist_ok=True)
    return {"path": full, "name": n}


def new_file(d, name):
    bad = check_name(name)
    if bad:
        return {"error": bad}
    n = free_name(d, name)
    full = join(d, n)
    with open(full, "xb"):
        pass
    return {"path": full, "name": n}


def rename_items(items):
    """Renames one or many. Names are checked first, and nothing is renamed if any new name is not
    allowed or would clash with a file that is not itself being renamed. Many are renamed in two
    steps (each to a temporary name, then to its new one), so names can be swapped without clashing."""
    items = list(items or [])
    problems = []
    frm = {str(it["path"]).rstrip(SEP) for it in items}
    targets = set()
    for it in items:
        p, nn = str(it["path"]).rstrip(SEP), str(it["name"])
        bad = check_name(nn)
        if bad:
            problems.append({"path": p, "error": bad})
            continue
        if not exists(p):
            problems.append({"path": p, "error": "It is no longer there."})
            continue
        t = join(os.path.dirname(p), nn)
        if t in targets:
            problems.append({"path": p, "error": "Two items would both be called " + nn + "."})
            continue
        targets.add(t)
        if exists(t) and t not in frm and t != p:
            problems.append({"path": p, "error": "Something called " + nn + " is already there."})
    if problems:
        return {"done": 0, "problems": problems}
    moves = []
    for it in items:
        p, nn = str(it["path"]).rstrip(SEP), str(it["name"])
        d = os.path.dirname(p)
        if os.path.basename(p) == nn:
            continue
        moves.append({"from": p, "tmp": join(d, ".fdren-" + uuid.uuid4().hex[:10]), "to": join(d, nn)})
    done = []
    try:
        for m in moves:
            os.rename(m["from"], m["tmp"])
            done.append(m)
        for m in moves:
            os.rename(m["tmp"], m["to"])
            m["finished"] = True
    except OSError as e:
        # put back whatever was moved, so a failure leaves the folder as it was
        for m in done:
            try:
                os.rename(m["to"] if m.get("finished") else m["tmp"], m["from"])
            except OSError:
                pass
        return {"done": 0, "problems": [{"path": "", "error": "Nothing was renamed: " + (e.strerror or str(e))}]}
    return {"done": len(moves), "problems": [], "renamed": [{"from": m["from"], "to": m["to"]} for m in moves]}


# ---- Deleting ------------------------------------------------------------------------------------
def recyclable(path):
    return True


def remove_tree(p):
    if os.path.islink(p) or not os.path.isdir(p):
        os.unlink(p)
    else:
        shutil.rmtree(p)


# ---- The Trash (freedesktop, shared with the file manager) -------------------------------------------
# Things deleted from your home drive go to ~/.local/share/Trash; things deleted from another drive (a USB
# stick, a second disk) go to a trash at the top of that drive, .Trash-<uid> or .Trash/<uid>, so they
# can be put back without copying. Atlas shows them all as one Trash.
def trash_dir():
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "Trash")


def trash_files():
    return os.path.join(trash_dir(), "files")


def trash_roots():
    """Every trash there is now: the home one first, then each mounted drive's own."""
    roots = [trash_dir()]
    uid = str(os.getuid())
    try:
        with open("/proc/self/mounts") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    for ln in lines:
        parts = ln.split()
        if len(parts) < 3 or parts[2] not in _REAL_FS:
            continue
        top = _unescape_mount(parts[1])
        for r in (os.path.join(top, ".Trash", uid), os.path.join(top, ".Trash-" + uid)):
            if r not in roots and os.path.isdir(os.path.join(r, "files")):
                roots.append(r)
    return roots


def _root_of(p):
    """The trash a trashed item is in (its folder's folder), or "" when it isn't directly in one."""
    par = os.path.dirname(os.path.normpath(p))
    if os.path.basename(par) != "files":
        return ""
    root = os.path.dirname(par)
    if root == trash_dir() or os.path.basename(root) == ".Trash-" + str(os.getuid()) or \
            (os.path.basename(root) == str(os.getuid()) and os.path.basename(os.path.dirname(root)) == ".Trash"):
        return root
    return ""


def in_trash(p):
    return bool(_root_of(p))


def _trash_top(root):
    """The drive a trash belongs to, which the paths in its .trashinfo files are relative to."""
    if root == trash_dir():
        return ""
    return os.path.dirname(os.path.dirname(root)) if os.path.basename(os.path.dirname(root)) == ".Trash" else os.path.dirname(root)


def trash_info(p):
    """Where a trashed item was, and when it was deleted (from its .trashinfo)."""
    out = {"path": "", "when": 0}
    root = _root_of(p) or trash_dir()
    try:
        with open(os.path.join(root, "info", os.path.basename(p) + ".trashinfo"), encoding="utf-8", errors="replace") as f:
            for ln in f:
                k, _, v = ln.strip().partition("=")
                if k == "Path":
                    v = urllib.parse.unquote(v)
                    out["path"] = v if v.startswith("/") else os.path.join(_trash_top(root), v)
                elif k == "DeletionDate":
                    try:
                        out["when"] = int(time.mktime(time.strptime(v[:19], "%Y-%m-%dT%H:%M:%S")) * 1000)
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def _forget_trash_info(p):
    try:
        os.remove(os.path.join(_root_of(p) or trash_dir(), "info", os.path.basename(p) + ".trashinfo"))
    except OSError:
        pass


def trash_items():
    """The full path of everything in every trash."""
    out = []
    for r in trash_roots():
        d = os.path.join(r, "files")
        try:
            out.extend(os.path.join(d, n) for n in os.listdir(d))
        except OSError:
            pass
    return out


def trash_count():
    return len(trash_items())


def list_trash(listing):
    """The home Trash's own listing, with every other drive's trash added in, and where each was."""
    full = listing["path"]
    for r in trash_roots()[1:]:
        d = os.path.join(r, "files")
        try:
            for de in children(d):
                try:
                    e = entry_of(de)
                except Exception:
                    continue
                e["p"] = os.path.join(d, e["n"])
                listing["entries"].append(e)
        except OSError:
            pass
    for e in listing["entries"]:
        info = trash_info(e.get("p") or join(full, e["n"]))
        e["o"], e["x"] = info["path"], info["when"]
    listing["trash"] = True
    return listing


def trash_restore(paths):
    """Put trashed items back where they were; a name now taken there gets a free one, as a copy would."""
    restored, problems = [], []
    for p in paths or []:
        p = str(p)
        if not in_trash(p) or not exists(p):
            continue
        orig = trash_info(p)["path"]
        if not orig:
            problems.append({"path": p, "error": "Atlas doesn't know where this was, so it was left in the Trash. Drag it where it belongs."})
            continue
        d = os.path.dirname(orig)
        try:
            os.makedirs(d, exist_ok=True)
            dest = join(d, free_name(d, os.path.basename(orig)))
            shutil.move(p, dest)
        except OSError as e:
            problems.append({"path": p, "error": e.strerror or str(e)})
            continue
        _forget_trash_info(p)
        restored.append({"from": p, "to": dest})
    return {"restored": restored, "problems": problems}


def trash_empty(state):
    removed, problems = 0, []
    for p in trash_items():
        if state and state.cancel:
            break
        try:
            remove_tree(p)
            _forget_trash_info(p)
            removed += 1
        except OSError as e:
            problems.append({"path": p, "error": e.strerror or str(e)})
    for r in trash_roots():
        try:
            os.remove(os.path.join(r, "directorysizes"))
        except OSError:
            pass
    return {"removed": removed, "problems": problems}


def trash(p):
    from gi.repository import Gio
    Gio.File.new_for_path(p).trash(None)


def _trash_snapshot():
    snap = {}
    for r in trash_roots():
        try:
            snap[r] = set(os.listdir(os.path.join(r, "info")))
        except OSError:
            snap[r] = set()
    return snap


def _trashed_since(snap):
    """What went into a trash since the snapshot: {original path: its place in the trash}."""
    out = {}
    for r in trash_roots():
        try:
            names = set(os.listdir(os.path.join(r, "info"))) - snap.get(r, set())
        except OSError:
            continue
        for nm in names:
            if nm.endswith(".trashinfo"):
                p = os.path.join(r, "files", nm[:-len(".trashinfo")])
                orig = trash_info(p)["path"]
                if orig:
                    out[orig] = p
    return out


def remove_items(paths, permanent, state):
    removed = 0
    problems = []
    snap = None if permanent else _trash_snapshot()
    for p in paths or []:
        if state and state.cancel:
            break
        p = str(p)
        try:
            if not exists(p):
                continue
            if permanent or in_trash(p):
                remove_tree(p)
                if in_trash(p):
                    _forget_trash_info(p)
            else:
                try:
                    trash(p)
                except Exception as e:
                    problems.append({"path": p, "error": "It could not be moved to the Trash (" + str(getattr(e, "message", e)) + "), so it was not deleted."})
                    continue
            removed += 1
        except OSError as e:
            problems.append({"path": p, "error": e.strerror or str(e)})
    trashed = []
    if snap is not None:
        went = _trashed_since(snap)
        trashed = [{"from": str(p), "to": went[os.path.normpath(str(p))]} for p in paths or [] if os.path.normpath(str(p)) in went]
    return {"removed": removed, "problems": problems, "trashed": trashed}


# ---- Copying and moving ---------------------------------------------------------------------------
def conflicts(sources, dest):
    return {"conflicts": [base_of(str(s)) for s in sources or [] if exists(join(dest, base_of(str(s))))]}


def copy(sources, dest, move, policy, state, dests=None):
    """Copies (or moves) items into a folder, reporting progress in state as it goes. policy says what
    to do when a name is already there: "replace", "skip" or "keep" (the new one gets a free name).
    A move within one file system is a rename, so it is instant; across file systems it is a copy,
    then the originals are removed once every file has copied. dests, when given, is a folder for each
    source in turn, made if it is not there."""
    policy = policy or "keep"
    state.phase = "counting"
    plan, problems, quick, tops = [], [], [], []
    total = count = 0
    src_list = list(sources or [])
    for si, src in enumerate(src_list):
        if state.cancel:
            break
        src = str(src)
        dest_full = os.path.abspath(str(dests[si]) if dests else dest)
        if dests and not exists(dest_full):
            try:
                os.makedirs(dest_full, exist_ok=True)
            except OSError as e:
                problems.append({"path": dest_full, "error": e.strerror or str(e)})
                continue
        src_full = src.rstrip(SEP) or SEP
        k = kind(src_full)
        if k == 0 and not os.path.islink(src_full):
            problems.append({"path": src, "error": "It is no longer there."})
            continue
        is_dir = k == 2 and not os.path.islink(src_full)
        name = os.path.basename(src_full)
        parent = os.path.dirname(src_full)
        if is_dir and (dest_full.rstrip(SEP) + SEP).startswith(src_full + SEP):
            problems.append({"path": src, "error": "A folder cannot be copied into itself."})
            continue
        same_folder = parent.rstrip(SEP) == dest_full.rstrip(SEP)
        if move and same_folder:
            continue
        target_name = name
        if exists(join(dest_full, name)):
            if policy == "skip" and not is_dir:
                continue
            if policy == "keep" or same_folder:
                target_name = free_name(dest_full, name)
        target = join(dest_full, target_name)
        tops.append({"from": src_full, "to": target, "replaced": exists(target)})
        # a move within one file system, to a name not taken: renamed at once
        try:
            same_vol = os.lstat(src_full).st_dev == os.stat(dest_full).st_dev
        except OSError:
            same_vol = False
        if move and same_vol and not exists(target):
            quick.append((src_full, target))
            continue
        if is_dir:
            plan.append(("dir", None, target, 0, 0))
            stack = [(src_full, target)]
            while stack:
                if state.cancel:
                    break
                a, b = stack.pop()
                try:
                    items = children(a)
                except OSError as e:
                    problems.append({"path": a, "error": e.strerror or str(e)})
                    continue
                for de in items:
                    t = join(b, de.name)
                    try:
                        if de.is_symlink():
                            plan.append(("link", de.path, t, 0, 0))
                        elif de.is_dir(follow_symlinks=False):
                            plan.append(("dir", None, t, 0, 0))
                            stack.append((de.path, t))
                        else:
                            st = de.stat(follow_symlinks=False)
                            plan.append(("file", de.path, t, st.st_size, st.st_mtime))
                            total += st.st_size
                            count += 1
                    except OSError as e:
                        problems.append({"path": de.path, "error": e.strerror or str(e)})
        elif os.path.islink(src_full):
            plan.append(("link", src_full, target, 0, 0))
        else:
            try:
                st = os.stat(src_full)
                ln, mt = st.st_size, st.st_mtime
            except OSError:
                ln, mt = 0, time.time()
            plan.append(("file", src_full, target, ln, mt))
            total += ln
            count += 1
        if move:
            plan.append(("remove", src_full, None, 0, 0))

    state.bytes_total, state.files_total = total, count
    state.bytes_done, state.files_done = 0, 0
    state.phase = "copying"

    for a, b in quick:
        try:
            os.rename(a, b)
        except OSError as e:
            problems.append({"path": a, "error": e.strerror or str(e)})

    failed_under = []
    for kind_, frm, to, size, mtime in plan:
        if state.cancel:
            break
        if kind_ == "dir":
            try:
                os.makedirs(to, exist_ok=True)
            except OSError as e:
                problems.append({"path": to, "error": e.strerror or str(e)})
            continue
        if kind_ == "link":
            try:
                if os.path.lexists(to):
                    if policy == "skip":
                        continue
                    os.unlink(to)
                os.symlink(os.readlink(frm), to)
            except OSError as e:
                problems.append({"path": frm, "error": e.strerror or str(e)})
                failed_under.append(frm)
            continue
        if kind_ == "remove":
            # the originals go only if every file under them copied
            if state.cancel or any(f == frm or f.startswith(frm + SEP) for f in failed_under):
                continue
            try:
                remove_tree(frm)
            except OSError as e:
                problems.append({"path": frm, "error": "Copied, but the original could not be removed: " + (e.strerror or str(e))})
            continue
        state.current = frm
        tmp = to + ".fdpart"
        ok = False
        try:
            if policy == "skip" and exists(to):
                state.files_done += 1
                state.bytes_done += size
                continue
            with open(frm, "rb") as fin, open(tmp, "wb") as fout:
                while True:
                    while state.paused and not state.cancel:
                        time.sleep(0.15)
                    if state.cancel:
                        break
                    buf = fin.read(1048576)
                    if not buf:
                        break
                    fout.write(buf)
                    state.bytes_done += len(buf)
            if state.cancel:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                break
            try:
                shutil.copymode(frm, tmp)
            except OSError:
                pass
            os.replace(tmp, to)
            try:
                os.utime(to, (time.time(), mtime))
            except OSError:
                pass
            ok = True
        except OSError as e:
            try:
                if exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
            problems.append({"path": frm, "error": e.strerror or str(e)})
            failed_under.append(frm)
        if ok:
            state.files_done += 1
    state.phase = "done"
    return {"filesDone": state.files_done, "filesTotal": count, "bytesDone": state.bytes_done, "bytesTotal": total,
            "moved": len(quick), "problems": problems, "cancelled": bool(state.cancel), "items": tops}


# ---- Reading a file for the preview pane ----------------------------------------------------------
def read_text(path, mx=262144):
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        buf = f.read(mx)
    got = len(buf)
    zeros = buf[:4096].count(0)
    enc = "utf-8"
    if buf[:2] == b"\xff\xfe":
        enc = "utf-16-le"
        buf = buf[2:]
    elif buf[:2] == b"\xfe\xff":
        enc = "utf-16-be"
        buf = buf[2:]
    elif zeros > 8:
        return {"binary": True, "size": size}
    text = buf.decode(enc, errors="replace")
    if text.startswith("﻿"):
        text = text[1:]
    return {"text": text, "size": size, "cut": size > got}


# ---- Details for the properties panel ---------------------------------------------------------------
def details(path):
    st = os.lstat(path)
    r = to_entry(os.path.basename(path.rstrip(SEP)) or SEP, path, st)
    r["path"] = path
    r["accessed"] = epoch_ms(st.st_atime)
    r["attributes"] = stat.filemode(st.st_mode)
    try:
        r["owner"] = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        r["owner"] = str(st.st_uid)
    if r["d"]:
        try:
            r["items"] = len(os.listdir(path))
        except OSError:
            pass
    return r


# ---- File hashes ---------------------------------------------------------------------------------------
def file_hash(path, state):
    """SHA-256, SHA-1 and MD5 in one read of the file (in capitals, as ThreatLocker and VirusTotal show them)"""
    algs = [hashlib.sha256(), hashlib.sha1(), hashlib.md5()]
    with open(path, "rb") as f:
        total = os.fstat(f.fileno()).st_size
        done = 0
        last = time.monotonic()
        while True:
            if state and state.cancel:
                return {"cancelled": True}
            buf = f.read(1048576)
            if not buf:
                break
            for a in algs:
                a.update(buf)
            done += len(buf)
            if time.monotonic() - last > 0.3:
                send_event(state, {"kind": "hash", "done": done, "total": total})
                last = time.monotonic()
    return {"sha256": algs[0].hexdigest().upper(), "sha1": algs[1].hexdigest().upper(), "md5": algs[2].hexdigest().upper(), "size": total}


# ---- Comparing two folders -------------------------------------------------------------------------------
# Files are the same when their sizes match and their modified times are within 2 seconds. A folder that is
# only on one side is listed once, not with everything inside it.
def _tree(root, state, scanned):
    out = {}
    stack = [""]
    while stack:
        if state and state.cancel:
            break
        rel = stack.pop()
        d = join(root, rel) if rel else root
        try:
            items = children(d)
        except OSError:
            continue
        for de in items:
            r = rel + SEP + de.name if rel else de.name
            try:
                isdir = de.is_dir(follow_symlinks=True)
                st = de.stat(follow_symlinks=True)
            except OSError:
                continue
            out[r] = {"rel": r, "d": isdir, "s": 0 if isdir else st.st_size, "m": st.st_mtime}
            scanned[0] += 1
            if isdir and not de.is_symlink():
                stack.append(r)
        if state and scanned[0] % 2000 < 20:
            send_event(state, {"kind": "compare", "scanned": scanned[0]})
    return out


def compare(left, right, state, mx=20000):
    scanned = [0]
    lt = _tree(left, state, scanned)
    rt = _tree(right, state, scanned)
    keys = sorted(set(lt) | set(rt), key=lambda k: k.lower())
    items = []
    counts = {"same": 0, "onlyLeft": 0, "onlyRight": 0, "newerLeft": 0, "newerRight": 0, "different": 0, "kinds": 0}
    skip = None
    for k in keys:
        if skip and k.startswith(skip):
            continue
        skip = None
        li, ri = lt.get(k), rt.get(k)
        if li and not ri:
            st = "onlyLeft"
            if li["d"]:
                skip = k + SEP
        elif ri and not li:
            st = "onlyRight"
            if ri["d"]:
                skip = k + SEP
        elif li["d"] and ri["d"]:
            continue
        elif li["d"] != ri["d"]:
            st = "kinds"
        else:
            dt = li["m"] - ri["m"]
            if abs(dt) <= 2 and li["s"] == ri["s"]:
                counts["same"] += 1
                continue
            st = ("newerLeft" if dt > 0 else "newerRight") if abs(dt) > 2 else "different"
        counts[st] += 1
        if len(items) < mx:
            one = li or ri
            items.append({"rel": one["rel"], "st": st, "d": bool(one["d"]),
                          "ls": li["s"] if li else None, "lm": epoch_ms(li["m"]) if li else None,
                          "rs": ri["s"] if ri else None, "rm": epoch_ms(ri["m"]) if ri else None})
    return {"left": left, "right": right, "items": items, "counts": counts, "scanned": scanned[0],
            "capped": len(items) >= mx, "cancelled": bool(state and state.cancel)}


# ---- Zip files ---------------------------------------------------------------------------------------------
# Archives: zip files are read with Python's zipfile; everything else (tar in all its kinds, 7z, rar, iso)
# with bsdtar (libarchive), which never writes outside the folder it is given.
import re as _re
ARCHIVE_RE = _re.compile(r"\.(zip|tar|tar\.gz|tgz|tar\.bz2|tbz2?|tar\.xz|txz|tar\.zst|tzst|tar\.lz|tar\.lzma|7z|rar|iso|cab)$", _re.I)


def is_archive(p):
    return bool(ARCHIVE_RE.search(p))


def find_zip_part(path):
    """A path that goes inside an archive (/home/me/Report.zip/Images): the archive, and where inside it."""
    parts = path.split(SEP)
    for i in range(1, len(parts)):
        z = SEP.join(parts[:i + 1])
        if is_archive(z) and kind(z) == 1:
            return {"zip": z, "inner": SEP.join(parts[i + 1:]).strip(SEP)} if i + 1 < len(parts) else None
    return None


_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_BSD_LINE = _re.compile(r"^(\S)\S{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w{3})\s+(\d+)\s+(\d{1,2}:\d{2}|\d{4}) (.*)$")


def _bsdtar():
    exe = shutil.which("bsdtar")
    if not exe:
        raise RuntimeError("Opening this kind of archive needs bsdtar (the libarchive package).")
    return exe


def archive_members(z):
    """Everything in an archive: (name, is folder, size, modified ms, packed size or None)."""
    if z.lower().endswith(".zip"):
        with zipfile.ZipFile(z) as za:
            return [(e.filename.replace("\\", "/"), e.filename.endswith("/"), e.file_size, _zip_time(e), e.compress_size) for e in za.infolist()]
    r = subprocess.run([_bsdtar(), "-tvf", z], capture_output=True, text=True, errors="replace", timeout=120)
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError((r.stderr.strip().splitlines() or ["bsdtar couldn't read it"])[-1].replace("bsdtar: ", ""))
    out, now = [], time.localtime()
    for ln in r.stdout.splitlines():
        m = _BSD_LINE.match(ln)
        if not m:
            continue
        typ, size, mon, day, ty, name = m.groups()
        if typ == "l" and " -> " in name:
            name = name.split(" -> ")[0]
        try:
            if ":" in ty:
                hh, mm = ty.split(":")
                stamp = time.mktime((now.tm_year, _MONTHS.get(mon, 1), int(day), int(hh), int(mm), 0, 0, 0, -1))
                if stamp > time.time() + 86400:
                    stamp = time.mktime((now.tm_year - 1, _MONTHS.get(mon, 1), int(day), int(hh), int(mm), 0, 0, 0, -1))
            else:
                stamp = time.mktime((int(ty), _MONTHS.get(mon, 1), int(day), 0, 0, 0, 0, 0, -1))
            ms = int(stamp * 1000)
        except (ValueError, OverflowError):
            ms = 0
        name = name[2:] if name.startswith("./") else name
        out.append((name, typ == "d" or name.endswith("/"), int(size), ms, None))
    return out


def _zip_time(info):
    try:
        return int(time.mktime(info.date_time + (0, 0, -1)) * 1000)
    except (OverflowError, ValueError):
        return 0


def zip_list(z, inner):
    """What is at one place inside a zip, listed like a folder. Entries carry "z" in their flags."""
    inner = (inner or "").strip("/")
    pre = inner + "/" if inner else ""
    try:
        members = archive_members(z)
    except (OSError, zipfile.BadZipFile, RuntimeError, subprocess.TimeoutExpired) as e:
        return {"path": z, "error": "This archive could not be opened: " + str(e)}
    if True:
        dirs, out = set(), []
        for fn, is_dir, size, ms, packed in members:
            if pre and not fn.lower().startswith(pre.lower()):
                continue
            rest = fn[len(pre):]
            if not rest:
                continue
            slash = rest.find("/")
            if slash >= 0 or is_dir:
                dn = rest[:slash] if slash >= 0 else rest
                if dn and dn.lower() not in dirs:
                    dirs.add(dn.lower())
                    out.append({"n": dn, "d": True, "s": 0, "m": 0, "c": 0, "a": "z"})
                continue
            out.append({"n": rest, "d": False, "s": size, "m": ms, "c": 0, "a": "z", "packed": packed})
    here = z + (SEP + inner.replace("/", SEP) if inner else "")
    return {"path": here, "parent": os.path.dirname(here), "entries": out, "zip": z, "inner": inner}


def new_zip(paths, d, name, state):
    """Puts files and folders in a new zip file in the folder given."""
    items, total = [], 0
    for p in paths or []:
        p = str(p).rstrip(SEP)
        base = os.path.basename(p)
        if kind(p) == 2:
            items.append((True, base + "/", None, 0))
            stack = [(p, base)]
            while stack:
                a, rel0 = stack.pop()
                for de in children(a):
                    rel = rel0 + "/" + de.name
                    if de.is_dir(follow_symlinks=False):
                        items.append((True, rel + "/", None, 0))
                        stack.append((de.path, rel))
                    elif de.is_file():
                        sz = de.stat().st_size
                        items.append((False, rel, de.path, sz))
                        total += sz
        elif kind(p) == 1:
            sz = os.path.getsize(p)
            items.append((False, base, p, sz))
            total += sz
    zn = free_name(d, name)
    zp = join(d, zn)
    state.bytes_total, state.bytes_done = total, 0
    ok = False
    try:
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as za:
            for isdir, rel, frm, _sz in items:
                if state.cancel:
                    break
                if isdir:
                    za.writestr(zipfile.ZipInfo(rel), b"")
                    continue
                zi = zipfile.ZipInfo.from_file(frm, rel)
                zi.compress_type = zipfile.ZIP_DEFLATED
                with open(frm, "rb") as fin, za.open(zi, "w") as fout:
                    while True:
                        buf = fin.read(1048576)
                        if not buf:
                            break
                        fout.write(buf)
                        state.bytes_done += len(buf)
                send_event(state, {"kind": "zip", "done": state.bytes_done, "total": total})
        ok = not state.cancel
    finally:
        if not ok:
            try:
                os.unlink(zp)
            except OSError:
                pass
    if not ok:
        return {"cancelled": True}
    return {"path": zp, "name": zn, "files": sum(1 for i in items if not i[0])}


def expand_zip(z, dest, entries, inner, state):
    """Takes files out of a zip: everything (into a new folder named after the zip), or what is selected
    inside it. Nothing is written outside the folder chosen, whatever names the zip holds."""
    if not z.lower().endswith(".zip"):
        return _expand_bsd(z, dest, entries, inner, state)
    files, problems = 0, []
    with zipfile.ZipFile(z) as za:
        if not dest:
            zd = os.path.dirname(z)
            dest = join(zd, free_name(zd, os.path.splitext(os.path.basename(z))[0]))
        os.makedirs(dest, exist_ok=True)
        dest_root = os.path.realpath(dest).rstrip(SEP) + SEP
        pre = (inner or "").strip("/")
        pre = pre + "/" if pre else ""
        want = [(pre + str(e).strip("/")).lower() for e in (entries or []) if e]
        infos = za.infolist()
        state.bytes_total, state.bytes_done = sum(i.file_size for i in infos), 0
        for e in infos:
            if state.cancel:
                break
            fn = e.filename.replace("\\", "/")
            low = fn.lower()
            if want and not any(low == w or low.startswith(w + "/") for w in want):
                continue
            rel = fn[len(pre):] if pre and low.startswith(pre.lower()) else fn
            if not rel:
                continue
            full = os.path.realpath(join(dest, rel.replace("/", SEP)))
            if not (full + (SEP if fn.endswith("/") else "")).startswith(dest_root) and full + SEP != dest_root:
                problems.append({"path": fn, "error": "Its name points outside the folder, so it was left out."})
                continue
            if fn.endswith("/"):
                os.makedirs(full, exist_ok=True)
                continue
            try:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                if exists(full):
                    full = join(os.path.dirname(full), free_name(os.path.dirname(full), os.path.basename(full)))
                with za.open(e) as fin, open(full, "wb") as fout:
                    while True:
                        buf = fin.read(1048576)
                        if not buf:
                            break
                        fout.write(buf)
                        state.bytes_done += len(buf)
                try:
                    t = time.mktime(e.date_time + (0, 0, -1))
                    os.utime(full, (t, t))
                except (OverflowError, ValueError, OSError):
                    pass
                files += 1
            except (OSError, zipfile.BadZipFile, RuntimeError) as ex:
                problems.append({"path": fn, "error": str(ex)})
    return {"dest": dest, "files": files, "problems": problems, "cancelled": bool(state.cancel)}


def _archive_stem(z):
    n = os.path.basename(z)
    m = ARCHIVE_RE.search(n)
    return n[:m.start()] if m and m.start() else n


def _expand_bsd(z, dest, entries, inner, state):
    if not dest:
        zd = os.path.dirname(z)
        dest = join(zd, free_name(zd, _archive_stem(z)))
    os.makedirs(dest, exist_ok=True)
    pre = (inner or "").strip("/")
    members = archive_members(z)
    want = [(pre + "/" if pre else "") + str(e).strip("/") for e in (entries or []) if e]
    if not want and pre:
        want = [pre]
    cmd = [_bsdtar(), "-xf", z, "-C", dest, "-k"]
    if pre:
        cmd += ["--strip-components", str(len(pre.split("/")))]
    cmd += want
    state.bytes_total, state.bytes_done = sum(m[2] for m in members), 0
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
    while p.poll() is None:
        if state.cancel:
            p.kill()
            break
        time.sleep(0.15)
    err = p.stderr.read() if p.stderr else ""
    problems = [{"path": z, "error": ln.replace("bsdtar: ", "")} for ln in err.strip().splitlines()[:20] if ln.strip()]
    sel = [m for m in members if not m[1] and (not want or any(m[0] == w or m[0].startswith(w + "/") for w in want))]
    state.bytes_done = state.bytes_total
    return {"dest": dest, "files": len(sel), "problems": problems, "cancelled": bool(state.cancel)}


def expand_zip_entry(z, entry):
    """One file from an archive, taken out to a temporary folder so it can be opened."""
    if not z.lower().endswith(".zip"):
        d = os.path.join(tempfile.gettempdir(), "Atlas", uuid.uuid4().hex[:8])
        os.makedirs(d, exist_ok=True)
        to = join(d, os.path.basename(entry.strip("/")))
        with open(to, "wb") as fout:
            r = subprocess.run([_bsdtar(), "-xOf", z, entry.strip("/")], stdout=fout, stderr=subprocess.PIPE, timeout=600)
        if r.returncode != 0:
            return {"error": "That file could not be taken out of the archive."}
        return {"path": to}
    with zipfile.ZipFile(z) as za:
        e = None
        want = entry.strip("/").lower()
        for x in za.infolist():
            if x.filename.replace("\\", "/").lower() == want:
                e = x
                break
        if not e:
            return {"error": "That file is no longer in the zip."}
        d = os.path.join(tempfile.gettempdir(), "Atlas", uuid.uuid4().hex[:8])
        os.makedirs(d, exist_ok=True)
        to = join(d, os.path.basename(e.filename.replace("\\", "/")))
        with za.open(e) as fin, open(to, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        return {"path": to}


# ---- Undoing a move: each item back where it was, under its old name (or a free one, if that's taken now)
def move_back(items, state):
    back, problems = [], []
    for it in items or []:
        if state and state.cancel:
            break
        frm, to = str(it.get("to") or ""), str(it.get("from") or "")
        if not frm or not exists(frm):
            problems.append({"path": frm, "error": "It is no longer there."})
            continue
        d = os.path.dirname(to)
        try:
            os.makedirs(d, exist_ok=True)
            dest = to if not exists(to) else join(d, free_name(d, os.path.basename(to)))
            shutil.move(frm, dest)
            back.append({"from": frm, "to": dest})
        except OSError as e:
            problems.append({"path": frm, "error": e.strerror or str(e)})
    return {"moved": back, "problems": problems}


# ---- Properties: who owns an item and who may do what with it -------------------------------------
def _user(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group(gid):
    import grp
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def perms(paths):
    items = []
    for p in paths or []:
        p = str(p)
        try:
            st = os.stat(p)
        except OSError:
            try:
                st = os.lstat(p)
            except OSError as e:
                items.append({"path": p, "error": e.strerror or str(e)})
                continue
        items.append({"path": p, "mode": st.st_mode & 0o7777, "d": stat.S_ISDIR(st.st_mode), "link": os.path.islink(p),
                      "owner": _user(st.st_uid), "group": _group(st.st_gid), "mine": st.st_uid == os.getuid(),
                      "size": st.st_size, "m": epoch_ms(st.st_mtime), "a": epoch_ms(st.st_atime), "c": epoch_ms(st.st_ctime)})
    groups = sorted({_group(g) for g in os.getgroups()} | {_group(os.getgid())})
    return {"items": items, "groups": groups, "me": _user(os.getuid())}


def chperm(paths, on, off, group, recursive, state):
    """Turns permission bits on and off (only the ones asked about, so a mixed selection keeps the rest),
    and changes the group. Inside folders, the run bits only change on folders, so files in them do
    not all become programs."""
    import grp
    on, off = int(on or 0) & 0o7777, int(off or 0) & 0o7777
    gid = -1
    if group:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError:
            raise RuntimeError("There is no group called " + group + ".")
    changed, problems = 0, []

    def apply(p, top):
        nonlocal changed
        if os.path.islink(p):
            return
        try:
            st = os.stat(p)
            a, b = on, off
            if not top and not stat.S_ISDIR(st.st_mode):
                a, b = a & ~0o111, b & ~0o111
            mode = st.st_mode & 0o7777
            new = (mode | a) & ~b
            did = False
            if new != mode:
                os.chmod(p, new)
                did = True
            if gid >= 0 and st.st_gid != gid:
                os.chown(p, -1, gid)
                did = True
            changed += did
        except OSError as e:
            problems.append({"path": p, "error": e.strerror or str(e)})

    for p in paths or []:
        p = str(p)
        apply(p, True)
        if recursive and os.path.isdir(p) and not os.path.islink(p):
            for d, dirs, files in os.walk(p):
                if state and state.cancel:
                    break
                for n in dirs + files:
                    apply(os.path.join(d, n), False)
    return {"changed": changed, "problems": problems[:50], "problemCount": len(problems)}


# ---- What's using a file, folder or drive ------------------------------------------------------------
def in_use(paths):
    """Programs with a file open in what was asked about (or working in it, or running from it), found by
    looking through /proc. Other people's and the system's programs can't be seen without admin rights."""
    want = [os.path.realpath(str(p)).rstrip(SEP) or SEP for p in paths or []]

    def hit(link):
        return any(link == w or link.startswith(w + SEP) for w in want)
    found, hidden = {}, 0
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        base = "/proc/" + pid
        how = set()
        try:
            for fd in os.listdir(base + "/fd"):
                try:
                    if hit(os.readlink(base + "/fd/" + fd)):
                        how.add("has a file open")
                except OSError:
                    pass
        except PermissionError:
            hidden += 1
            continue
        except OSError:
            continue
        try:
            if hit(os.readlink(base + "/cwd")):
                how.add("is working in it")
        except OSError:
            pass
        try:
            if hit(os.readlink(base + "/exe")):
                how.add("is running from it")
        except OSError:
            pass
        if not how:
            try:
                with open(base + "/maps") as f:
                    for ln in f:
                        i = ln.find(" /")
                        if i > 0 and hit(ln[i + 1:].rstrip("\n").replace(" (deleted)", "")):
                            how.add("has it loaded")
                            break
            except OSError:
                pass
        if how:
            try:
                with open(base + "/comm") as f:
                    name = f.read().strip()
            except OSError:
                name = pid
            try:
                exe = os.readlink(base + "/exe")
            except OSError:
                exe = ""
            try:
                owner = _user(os.stat(base).st_uid)
            except OSError:
                owner = ""
            found[int(pid)] = {"pid": int(pid), "name": name, "exe": exe, "owner": owner, "how": ", ".join(sorted(how))}
    return {"programs": sorted(found.values(), key=lambda x: x["name"].lower()), "checked": 0, "hidden": hidden}


def end_program(pid):
    import signal
    pid = int(pid)
    if pid <= 1 or pid == os.getpid():
        raise RuntimeError("Atlas won't end that one.")
    os.kill(pid, signal.SIGTERM)
    return True


# ---- Wallpaper (Omarchy) ----------------------------------------------------------------------------
def set_wallpaper(path):
    exe = shutil.which("omarchy-theme-bg-set") or os.path.join(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy"), "bin", "omarchy-theme-bg-set")
    if not os.path.isfile(exe):
        raise RuntimeError("Setting the wallpaper needs Omarchy (omarchy-theme-bg-set was not found).")
    r = subprocess.run([exe, str(path)], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "Omarchy couldn't set it as the wallpaper.")
    return True


# ---- Requests from the page that run in the background ---------------------------------------------
WINDOWS_ONLY = {"shares", "access", "members", "letters", "mapdrive", "unmapdrive"}


def run(op, a, state):
    """One background request: the page's op and its arguments; returns what the page is answered."""
    if op == "list":
        return list_dir(str(a.get("path") or ""))
    if op == "drives":
        return drives()
    if op == "moveback":
        return move_back(a.get("items"), state)
    if op == "perms":
        return perms(a.get("paths"))
    if op == "chperm":
        return chperm(a.get("paths"), a.get("on"), a.get("off"), str(a.get("group") or ""), bool(a.get("recursive")), state)
    if op == "inuse":
        return in_use(a.get("paths"))
    if op == "endprogram":
        return end_program(a.get("pid"))
    if op == "wallpaper":
        return set_wallpaper(str(a.get("path") or ""))
    if op == "restore":
        return trash_restore(a.get("paths"))
    if op == "emptytrash":
        return trash_empty(state)
    if op == "smbshares":
        return smb_shares(str(a.get("server") or ""), str(a.get("user") or ""), str(a.get("domain") or ""), str(a.get("password") or ""))
    if op == "search":
        return search(str(a.get("root") or ""), str(a.get("query") or ""), a.get("options"), state)
    if op == "sizes":
        return sizes(a.get("paths"), state)
    if op == "children":
        return children_sizes(str(a.get("path") or ""), state)
    if op == "rename":
        return rename_items(a.get("items"))
    if op == "mkdir":
        return new_folder(str(a.get("dir")), str(a.get("name")))
    if op == "newfile":
        return new_file(str(a.get("dir")), str(a.get("name")))
    if op == "delete":
        return remove_items(a.get("paths"), bool(a.get("permanent")), state)
    if op == "conflicts":
        return conflicts(a.get("sources"), str(a.get("dest")))
    if op == "readtext":
        return read_text(str(a.get("path")))
    if op == "details":
        return details(str(a.get("path")))
    if op == "recyclable":
        return {"ok": recyclable(str(a.get("path")))}
    if op == "copy":
        return copy(a.get("sources"), str(a.get("dest") or ""), bool(a.get("move")), str(a.get("policy") or ""), state, a.get("dests"))
    if op == "hash":
        return file_hash(str(a.get("path")), state)
    if op == "compare":
        return compare(str(a.get("left")), str(a.get("right")), state)
    if op == "zip":
        return new_zip(a.get("paths"), str(a.get("dir")), str(a.get("name")), state)
    if op == "unzip":
        return expand_zip(str(a.get("zip")), str(a.get("dest") or ""), a.get("entries"), str(a.get("inner") or ""), state)
    if op == "zipopen":
        return expand_zip_entry(str(a.get("zip")), str(a.get("entry")))
    if op in WINDOWS_ONLY:
        raise RuntimeError("This only works on Windows.")
    raise RuntimeError("Unknown request: " + op)
