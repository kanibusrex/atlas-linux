"""
atlas_core.py: Atlas's file operations. A port of AtlasCore.ps1 with the same requests and answers, so
atlas.html works unchanged. No windows here, only files, so every function can run in a background
thread and be tested on its own.

What works the same everywhere is here. What each system does its own way (its drives, its Trash,
your Documents folder, which files are hidden...) comes from atlas_core_linux.py, or the file for
the system Atlas is running on, as `system`.

Every function that can take a while is given a State holding:
  events   a queue of progress messages for the window to pass on to the page
  cancel   set to True by the window to stop the work
  paused   set to True by the window to pause a copy
  pace     how hard a copy to a network share may push it (PACES), set by the window from the settings
"""

import errno
import fnmatch
import hashlib
import os
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
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
        self.pace = "full"
        self.note = ""


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
    if system.is_hidden(name, st):
        flags += "h"
    if not os.access(full, os.W_OK):
        flags += "r"
    if link:
        flags += "l"
    m = epoch_ms(st.st_mtime) if st else 0
    c = 0
    if st:
        c = epoch_ms(getattr(st, "st_birthtime", 0) or st.st_ctime)
    out = {"n": name, "d": is_dir, "s": 0 if is_dir or not st else st.st_size, "m": m, "c": c, "a": flags}
    # in the cloud, not yet on this computer: looking inside it would download it
    if st and not is_dir and not link and system.online_only(full, st):
        out["cl"] = 1
    return out


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



def resolve_path(path):
    """A typed path: ~ for your own folder, $NAME and %NAME% environment names, shell: names"""
    p = str(path or "").strip().strip('"')
    if p.lower().startswith("shell:"):
        name, _, rest = p[6:].partition("/")
        key = name.lower().replace(" ", "")
        if key in _SHELL:
            p = os.path.join(system.user_dir(_SHELL[key]), rest) if rest else system.user_dir(_SHELL[key])
        elif key in ("profile", "home"):
            p = os.path.join(os.path.expanduser("~"), rest) if rest else os.path.expanduser("~")
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
    if not os.path.isabs(p):
        return {"path": p, "error": "Type a full path, such as " + system.PATH_HINT + "."}
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
        par = "" if os.path.dirname(full) == full else os.path.dirname(full)
        return system.listing(full, {"path": full, "parent": par, "entries": entries, "long": False})
    except PermissionError:
        return {"path": p, "error": "You do not have permission to open this folder."}
    except OSError as e:
        return {"path": p, "error": e.strerror or str(e)}


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
            if not hidden and system.hidden_entry(de):
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
    return system.bad_name(n)


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
def remove_tree(p):
    if os.path.islink(p) or not os.path.isdir(p):
        os.unlink(p)
    else:
        shutil.rmtree(p)


# ---- Copying and moving ---------------------------------------------------------------------------
def conflicts(sources, dest):
    return {"conflicts": [base_of(str(s)) for s in sources or [] if exists(join(dest, base_of(str(s))))]}


# ---- Copies to a network share: paced, and picked up again when it drops --------------------------------
# A share can be a USB stick in a router: a long copy at full speed heats the stick until the router
# loses it. So a copy to a share can be paced (rests, a speed limit), is written through to the share as
# it goes (so a rest really is a rest), and when the share drops it waits for it and carries on from the
# last part known to be there, instead of failing.
PACES = {   # pace: (bytes written before a rest, the rest in seconds, a speed limit in bytes a second or 0)
    "full": (0, 0, 0),
    "gentle": (512 << 20, 20, 0),
    "gentlest": (256 << 20, 45, 6 << 20),
}
CHUNK = 1 << 20
SYNC_EVERY = 32 << 20      # written through to the share this often, on a network share
RECONNECT_FOR = 600        # seconds to wait for a share that has dropped
COOL_AFTER_DROP = 30       # and a rest once it is back, before writing to it again
TRIES = 8                  # times one file is picked up again before it counts as failed


def _nap(state, secs, phase=None):
    """Sleeps for a while, unless the copy is stopped. False when it was."""
    if phase:
        state.phase = phase
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if state.cancel:
            return False
        time.sleep(min(0.2, max(0.0, end - time.monotonic())))
    if phase:
        state.phase = "copying"
    return True


def _answers(path, wait=8.0):
    """Can this folder be read? Asked in a thread: a share that has gone can hang a call for minutes."""
    box = {}

    def go():
        try:
            os.listdir(path)
            box["ok"] = True
        except OSError:
            pass
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(wait)
    return bool(box.get("ok"))


def _wait_back(folder, state, err):
    """After a share dropped: waits (reconnecting now and then) until the folder can be read again."""
    state.note = "The share dropped (%s). Waiting for it to come back..." % ((getattr(err, "strerror", None) or str(err)) or "no answer")
    state.phase = "reconnecting"
    reconnect = getattr(system, "reconnect", None)
    start, n = time.monotonic(), 0
    while time.monotonic() - start < RECONNECT_FOR:
        if state.cancel:
            return False
        if n and not _nap(state, 5):
            return False
        if reconnect and n % 3 == 1:
            try:
                reconnect(folder)
            except Exception:
                pass
        n += 1
        if _answers(folder):
            state.note = "The share came back. Letting it cool down before carrying on."
            ok = _nap(state, COOL_AFTER_DROP if n > 1 else 3, "resting")
            state.note = ""
            return ok
    state.note = ""
    state.phase = "copying"
    return False


def _sync(f):
    f.flush()
    try:
        os.fsync(f.fileno())
    except OSError as e:
        if e.errno not in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP):
            raise


class _Pacer:
    """Rests and the speed limit for the copy's pace, counted over the whole copy (not per file)."""

    def __init__(self, state, on):
        self.state, self.on = state, on
        self.since_rest = 0
        self.t0, self.sent = time.monotonic(), 0

    def wrote(self, n, sync):
        if not self.on:
            return True
        every, rest, cap = PACES.get(self.state.pace, PACES["full"])
        self.since_rest += n
        self.sent += n
        if cap:
            ahead = self.sent / cap - (time.monotonic() - self.t0)
            if ahead > 0 and not _nap(self.state, ahead):
                return False
        if every and self.since_rest >= every:
            sync()
            self.state.note = "Resting for %d s so the share can cool down." % rest
            ok = _nap(self.state, rest, "resting")
            self.state.note = ""
            self.since_rest, self.t0, self.sent = 0, time.monotonic(), 0
            return ok
        return True


def _same_block(frm, tmp, at):
    """Do the source and the part copied so far hold the same bytes just before at?"""
    try:
        with open(frm, "rb") as a, open(tmp, "rb") as b:
            a.seek(at - CHUNK)
            b.seek(at - CHUNK)
            return a.read(CHUNK) == b.read(CHUNK)
    except OSError:
        return False


def _copy_file(frm, tmp, state, pacer, slow):
    """Copies one file into tmp. On a share, a drop is waited out and the file picked up where it was.
    True when copied, False when stopped; raises OSError when it could not be copied."""
    done = good = 0          # bytes in tmp, and bytes known to be on the share (written through)
    tries = 0
    while True:
        try:
            with open(frm, "rb") as fin, open(tmp, "r+b" if done else "wb") as fout:
                if done:
                    fout.truncate(done)
                    fout.seek(done)
                    fin.seek(done)
                unsynced = 0
                while True:
                    while state.paused and not state.cancel:
                        time.sleep(0.15)
                    if state.cancel:
                        return False
                    buf = fin.read(CHUNK)
                    if not buf:
                        break
                    fout.write(buf)
                    done += len(buf)
                    unsynced += len(buf)
                    state.bytes_done += len(buf)
                    if slow and unsynced >= SYNC_EVERY:
                        _sync(fout)
                        good, unsynced = done, 0
                    if not pacer.wrote(len(buf), lambda: _sync(fout)):
                        return False
                if slow:
                    _sync(fout)
            return True
        except OSError as e:
            tries += 1
            if not slow or state.cancel or tries > TRIES:
                raise
            if not _wait_back(os.path.dirname(tmp), state, e):
                if state.cancel:
                    return False
                raise
            # carry on from the last part known to be there, if it really is the same as the source
            try:
                there = os.path.getsize(tmp)
            except OSError:
                there = 0
            at = min(good, there) // CHUNK * CHUNK
            if at and not _same_block(frm, tmp, at):
                at = 0
            state.bytes_done -= done - at
            done = good = at


def _again(fn, folder, state, slow):
    """Does a small step (a folder made, a file put in place), waiting out a dropped share on the way."""
    for n in range(TRIES + 1):
        try:
            return fn()
        except OSError as e:
            if not slow or n == TRIES or not _wait_back(folder, state, e):
                raise


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
    slow_target = getattr(system, "slow_target", None)
    slow = False
    if slow_target:
        for d in {os.path.abspath(str(x)) for x in (dests or [dest])}:
            try:
                slow = slow or bool(slow_target(d))
            except Exception:
                pass
    pacer = _Pacer(state, slow)

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
                _again(lambda: os.makedirs(to, exist_ok=True), os.path.dirname(to.rstrip(SEP)), state, slow)
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
            _copy_file(frm, tmp, state, pacer, slow)
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
            _again(lambda: os.replace(tmp, to), os.path.dirname(to), state, slow)
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
    r["owner"] = system.owner(st.st_uid)
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
    path = _re.sub("/{2,}", "/", path)
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
        # macOS's tar, and the tar.exe in Windows 10 and later, are bsdtar under another name
        tar = shutil.which("tar")
        try:
            if tar and "bsdtar" in subprocess.run([tar, "--version"], capture_output=True, text=True, timeout=5).stdout:
                exe = tar
        except (OSError, subprocess.SubprocessError):
            pass
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
                      "owner": system.owner(st.st_uid), "group": _group(st.st_gid), "mine": st.st_uid == os.getuid(),
                      "size": st.st_size, "m": epoch_ms(st.st_mtime), "a": epoch_ms(st.st_atime), "c": epoch_ms(st.st_ctime)})
    groups = sorted({_group(g) for g in os.getgroups()} | {_group(os.getgid())})
    return {"items": items, "groups": groups, "me": system.owner(os.getuid())}


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


# ---- Requests from the page that run in the background ---------------------------------------------
OPS = {
    "list": lambda a, state: list_dir(str(a.get("path") or "")),
    "moveback": lambda a, state: move_back(a.get("items"), state),
    "perms": lambda a, state: perms(a.get("paths")),
    "chperm": lambda a, state: chperm(a.get("paths"), a.get("on"), a.get("off"), str(a.get("group") or ""), bool(a.get("recursive")), state),
    "search": lambda a, state: search(str(a.get("root") or ""), str(a.get("query") or ""), a.get("options"), state),
    "sizes": lambda a, state: sizes(a.get("paths"), state),
    "children": lambda a, state: children_sizes(str(a.get("path") or ""), state),
    "rename": lambda a, state: rename_items(a.get("items")),
    "mkdir": lambda a, state: new_folder(str(a.get("dir")), str(a.get("name"))),
    "newfile": lambda a, state: new_file(str(a.get("dir")), str(a.get("name"))),
    "conflicts": lambda a, state: conflicts(a.get("sources"), str(a.get("dest"))),
    "readtext": lambda a, state: read_text(str(a.get("path"))),
    "details": lambda a, state: details(str(a.get("path"))),
    "copy": lambda a, state: copy(a.get("sources"), str(a.get("dest") or ""), bool(a.get("move")), str(a.get("policy") or ""), state, a.get("dests")),
    "hash": lambda a, state: file_hash(str(a.get("path")), state),
    "compare": lambda a, state: compare(str(a.get("left")), str(a.get("right")), state),
    "zip": lambda a, state: new_zip(a.get("paths"), str(a.get("dir")), str(a.get("name")), state),
    "unzip": lambda a, state: expand_zip(str(a.get("zip")), str(a.get("dest") or ""), a.get("entries"), str(a.get("inner") or ""), state),
    "zipopen": lambda a, state: expand_zip_entry(str(a.get("zip")), str(a.get("entry"))),
}


def handles(op):
    """Whether op is a request answered here (in the background)."""
    return op in OPS or op in system.OPS or op in system.NOT_HERE


def run(op, a, state):
    """One background request: the page's op and its arguments; returns what the page is answered.
    The system's own answer comes first, where it has one."""
    fn = system.OPS.get(op) or OPS.get(op)
    if fn:
        return fn(a, state)
    if op in system.NOT_HERE:
        raise RuntimeError("This only works on " + system.ELSEWHERE + ".")
    raise RuntimeError("Unknown request: " + op)


# ---- The system Atlas is running on (last: its file uses what is above) -------------------------------
if sys.platform.startswith("linux"):
    import atlas_core_linux as system  # noqa: E402
elif sys.platform == "win32":
    import atlas_core_windows as system  # noqa: E402
elif sys.platform == "darwin":
    import atlas_core_mac as system  # noqa: E402
else:
    raise ImportError("Atlas has no file work for " + sys.platform + " yet.")
user_dir = system.user_dir
