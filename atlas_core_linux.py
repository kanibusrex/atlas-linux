"""
atlas_core_linux.py: the file work that is Linux's own: drives (from /proc/self/mounts), network places
connected through GVFS, the freedesktop Trash, what programs are using a file (from /proc), and Omarchy's
wallpaper. atlas_core.py takes from here what it can't do the same way everywhere; a Mac or Windows
version of this file gives the same names.

Like atlas_core.py, no windows here, only files: everything can run in a background thread.
"""

import os
import pwd
import shutil
import subprocess
import threading
import time
import urllib.parse

import atlas_cloud
import atlas_core as core


# ---- Your own folders: Documents, Downloads... (from ~/.config/user-dirs.dirs) --------------------------
def user_dir(key):
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

# ---- Drives and the usual places ----------------------------------------------------------------
# (rclone's mounts are in cloud_places, and have their own cache: no need to pace copies to them)
_NET_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "sshfs", "fuse.sshfs", "davfs", "fuse.davfs2", "9p"}
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
    for name, p in (("Home", home), ("Documents", user_dir("DOCUMENTS")), ("Downloads", user_dir("DOWNLOAD")),
                    ("Pictures", user_dir("PICTURES")), ("Videos", user_dir("VIDEOS"))):
        if p and os.path.isdir(p) and not any(x["path"] == p for x in places):
            places.append({"name": name, "path": p})
    try:
        os.makedirs(trash_files(), exist_ok=True)
        os.makedirs(os.path.join(trash_dir(), "info"), exist_ok=True)
    except OSError:
        pass
    return {"drives": out, "clouds": cloud_places(), "places": places, "trash": {"path": trash_files(), "count": trash_count()}, "user": pwd.getpwuid(os.getuid()).pw_name, "machine": os.uname().nodename}


# ---- Cloud accounts (atlas_cloud.py), and any other rclone mounts there are --------------------------------
def cloud_places():
    now = atlas_cloud.mounts()
    out = []
    for acct in atlas_cloud.accounts():
        path = os.path.join(atlas_cloud.MOUNTS, acct["name"])
        info = {"path": path, "label": acct["name"], "service": acct.get("service", ""), "own": True, "mounted": path in now,
                "ready": False, "total": 0, "free": 0, "problem": atlas_cloud.problem(acct["name"])}
        sv = _statvfs(path) if path in now else None
        if sv:
            info["ready"] = True
            info["total"] = sv.f_blocks * sv.f_frsize
            info["free"] = sv.f_bavail * sv.f_frsize
        out.append(info)
    known = {x["path"] for x in out}
    for path, src in now.items():
        if path not in known:
            out.append({"path": path, "label": os.path.basename(path) or src, "service": "", "own": False, "mounted": True, "ready": True, "total": 0, "free": 0})
    return out


def online_only(path, st):
    return atlas_cloud.online_only(path, st)


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
    # -s /dev/null: no /etc/samba/smb.conf is needed (most desktops have none, and smbclient then warns
    # "Can't load /etc/samba/smb.conf", which used to be shown as the error).
    cmd = [exe, "-s", "/dev/null", "-L", "//" + server, "-g"]
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
        msg = [ln.strip() for ln in (r.stderr + "\n" + r.stdout).splitlines()
               if ln.strip() and "smb.conf" not in ln and "testparm" not in ln]
        msg = next((ln for ln in reversed(msg) if "NT_STATUS_" in ln), msg[-1] if msg else "smbclient failed.")
        if "NT_STATUS_LOGON_FAILURE" in msg or "NT_STATUS_ACCESS_DENIED" in msg:
            msg = "The server turned down that user name and password."
        elif "NT_STATUS_" in msg and ("UNSUCCESSFUL" in msg or "NOT_FOUND" in msg or "TIMEOUT" in msg or "UNREACHABLE" in msg or "REFUSED" in msg):
            msg = "Couldn't reach " + server + "."
        raise RuntimeError(msg)
    return {"server": server, "shares": shares}

def recyclable(path):
    return True


# ---- Copies to a network place: paced, and picked up again when it drops (atlas_core.copy) -----------
def slow_target(path):
    """Is this folder on a network share (GVFS, or a cifs/nfs/sshfs... mount)?"""
    path = os.path.abspath(path)
    root = gvfs_root()
    if path == root or path.startswith(root + os.sep):
        return True
    best, fs = "", ""
    try:
        with open("/proc/self/mounts") as f:
            for ln in f:
                parts = ln.split()
                if len(parts) < 3:
                    continue
                mnt = _unescape_mount(parts[1])
                if (path == mnt or path.startswith(mnt.rstrip(os.sep) + os.sep)) and len(mnt) > len(best):
                    best, fs = mnt, parts[2]
    except OSError:
        return False
    return fs in _NET_FS


def reconnect(path):
    """Mounts a GVFS share again after it dropped (with the password in the keyring), so a copy can carry on."""
    root = gvfs_root()
    path = os.path.abspath(path)
    if not path.startswith(root + os.sep):
        return
    nm = path[len(root) + 1:].split(os.sep, 1)[0]
    share = next((x for x in gvfs_shares() if os.path.basename(x["path"]) == nm), None)
    uri = share["unc"] if share else ""
    if not uri:   # gone from the list: rebuild smb://[user@]server/share from the folder's name
        kind_, _, spec = nm.partition(":")
        kv = dict(part.partition("=")[::2] for part in spec.split(","))
        if kind_ != "smb-share" or not kv.get("server") or not kv.get("share"):
            return
        uri = "smb://" + (kv["user"] + "@" if kv.get("user") else "") + kv["server"] + "/" + kv["share"]
    exe = shutil.which("gio")
    if exe:
        try:
            subprocess.run([exe, "mount", uri], stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass


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
            for de in core.children(d):
                try:
                    e = core.entry_of(de)
                except Exception:
                    continue
                e["p"] = os.path.join(d, e["n"])
                listing["entries"].append(e)
        except OSError:
            pass
    for e in listing["entries"]:
        info = trash_info(e.get("p") or core.join(full, e["n"]))
        e["o"], e["x"] = info["path"], info["when"]
    listing["trash"] = True
    return listing


def trash_restore(paths):
    """Put trashed items back where they were; a name now taken there gets a free one, as a copy would."""
    restored, problems = [], []
    for p in paths or []:
        p = str(p)
        if not in_trash(p) or not core.exists(p):
            continue
        orig = trash_info(p)["path"]
        if not orig:
            problems.append({"path": p, "error": "Atlas doesn't know where this was, so it was left in the Trash. Drag it where it belongs."})
            continue
        d = os.path.dirname(orig)
        try:
            os.makedirs(d, exist_ok=True)
            dest = core.join(d, core.free_name(d, os.path.basename(orig)))
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
            core.remove_tree(p)
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
    if atlas_cloud.on_mount(p):
        return core.remove_tree(p)     # a Trash folder there would be uploaded; the service keeps its own
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
            if not core.exists(p):
                continue
            if permanent or in_trash(p):
                core.remove_tree(p)
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


# ---- What's using a file, folder or drive ------------------------------------------------------------
def in_use(paths):
    """Programs with a file open in what was asked about (or working in it, or running from it), found by
    looking through /proc. Other people's and the system's programs can't be seen without admin rights."""
    want = [os.path.realpath(str(p)).rstrip(core.SEP) or core.SEP for p in paths or []]

    def hit(link):
        return any(link == w or link.startswith(w + core.SEP) for w in want)
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
                owner = core._user(os.stat(base).st_uid)
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


# ---- What atlas_core.py asks of every system --------------------------------------------------------
PATH_HINT = "~/Documents or /tmp"
ELSEWHERE = "Windows"   # where the requests in NOT_HERE work


def is_hidden(name, _st):
    return name.startswith(".")


def hidden_entry(de):
    return de.name.startswith(".")


def bad_name(_n):
    """Anything besides / is allowed in a name here (atlas_core checks that, and the length)."""
    return ""


def owner(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def listing(full, out):
    """A folder's listing as the page is given it: the Trash shows where each thing was, and when it went."""
    return list_trash(out) if full == trash_files() else out


# requests answered here, each given the page's arguments and the request's State
OPS = {
    "drives": lambda a, state: drives(),
    "delete": lambda a, state: remove_items(a.get("paths"), bool(a.get("permanent")), state),
    "recyclable": lambda a, state: {"ok": recyclable(str(a.get("path")))},
    "restore": lambda a, state: trash_restore(a.get("paths")),
    "emptytrash": lambda a, state: trash_empty(state),
    "smbshares": lambda a, state: smb_shares(str(a.get("server") or ""), str(a.get("user") or ""), str(a.get("domain") or ""), str(a.get("password") or "")),
    "inuse": lambda a, state: in_use(a.get("paths")),
    "endprogram": lambda a, state: end_program(a.get("pid")),
    "wallpaper": lambda a, state: set_wallpaper(str(a.get("path") or "")),
    "cloudinfo": lambda a, state: atlas_cloud.info(),
    "cloudconnect": lambda a, state: atlas_cloud.connect(a, state),
    "cloudabandon": lambda a, state: atlas_cloud.abandon(str(a.get("name") or "")),
    "cloudmount": lambda a, state: {"problem": atlas_cloud.mount(str(a.get("name") or ""))},
    "clouddisconnect": lambda a, state: atlas_cloud.disconnect(str(a.get("name") or ""), bool(a.get("force"))),
}

# the Windows Atlas's own: mapped drives, shares a PC offers, and who has access (Active Directory)
NOT_HERE = {"shares", "access", "members", "letters", "mapdrive", "unmapdrive"}
