"""
Cloud accounts on Linux, where OneDrive, Google and Apple offer no app of their own: rclone (from the system's
packages) signs in and shows each account as a folder under ~/Cloud, for Atlas and every other app.

Atlas keeps a list of the accounts and an rclone config of its own (its sign-ins encrypted, with a random password
kept in the keyring, like a network share's password). Files are fetched when they are opened, and kept for a while
in ~/.cache/atlas/rclone. On Windows and macOS the services' own apps make these folders; see cloud_places there.
"""

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time

# what can be connected, as rclone calls them
SERVICES = {"onedrive": "OneDrive", "drive": "Google Drive", "dropbox": "Dropbox", "box": "Box", "iclouddrive": "iCloud Drive"}
# what each may be told up front, of what the person typed in Atlas's dialog
FIELDS = {"drive": ("client_id", "client_secret"), "iclouddrive": ("apple_id",)}

_HOME = os.path.expanduser("~")
ROOT = os.environ.get("ATLAS_CLOUD_DIR") or os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(_HOME, ".local/share")), "atlas", "cloud")
MOUNTS = os.environ.get("ATLAS_CLOUD_MOUNTS") or os.path.join(_HOME, "Cloud")
CACHE = os.environ.get("ATLAS_CLOUD_CACHE") or os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(_HOME, ".cache")), "atlas", "rclone")
CONF = os.path.join(ROOT, "rclone.conf")
ACCOUNTS = os.path.join(ROOT, "accounts.json")
LOG = os.path.join(ROOT, "rclone.log")
SIGN_IN_WAIT = 300   # seconds a sign-in in the browser may take
_lock = threading.Lock()
_problems = {}       # why an account couldn't be mounted


def exe():
    return os.environ.get("ATLAS_RCLONE") or shutil.which("rclone")


# ---- The accounts, and the config with their sign-ins ----------------------------------------------------
def accounts():
    try:
        with open(ACCOUNTS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_accounts(items):
    tmp = ACCOUNTS + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f)
    os.replace(tmp, ACCOUNTS)


def _key_args():
    return ["secret-tool", "lookup", "application", "atlas-cloud", "config", CONF]


def _password_command():
    return " ".join('"%s"' % a if " " in a else a for a in _key_args())


def _ensure_config():
    """The config, encrypted when there's a keyring to hold its password (otherwise only you can read the file)."""
    os.makedirs(ROOT, mode=0o700, exist_ok=True)
    if os.path.exists(CONF):
        return
    with open(CONF, "w"):
        pass
    os.chmod(CONF, 0o600)
    if not shutil.which("secret-tool"):
        return
    try:
        word = secrets.token_urlsafe(32)
        subprocess.run(["secret-tool", "store", "--label=Atlas cloud accounts", "application", "atlas-cloud", "config", CONF],
                       input=word, text=True, capture_output=True, timeout=30, check=True)
        got = subprocess.run(_key_args(), capture_output=True, text=True, timeout=30)
        if got.stdout.strip() != word:
            return
        env = dict(os.environ, RCLONE_PASSWORD_CHANGE="1")
        subprocess.run([exe(), "--config", CONF, "--password-command", _password_command(), "config", "encryption", "set"],
                       env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        pass


def _encrypted():
    try:
        with open(CONF, encoding="utf-8", errors="replace") as f:
            return f.readline().startswith("# Encrypted rclone")
    except OSError:
        return False


def _base():
    return [exe(), "--config", CONF] + (["--password-command", _password_command()] if _encrypted() else [])


def _forget_config():
    """With no accounts left, nothing of them stays: the config and its password in the keyring."""
    try:
        os.remove(CONF)
    except OSError:
        pass
    if shutil.which("secret-tool"):
        try:
            subprocess.run(["secret-tool", "clear", "application", "atlas-cloud", "config", CONF], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass


# ---- Signing in: rclone asks its questions one at a time, and Atlas answers all but those only you can --------
def _run(cmd, state, timeout, on_line=None):
    """Runs rclone until it ends, is cancelled or runs out of time: (exit code or None, stdout, stderr)."""
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd="/")
    out, err = [], []

    def read_err():
        for ln in p.stderr:
            err.append(ln)
            if on_line:
                on_line(ln)

    def read_out():
        out.append(p.stdout.read())
    ths = [threading.Thread(target=read_err, daemon=True), threading.Thread(target=read_out, daemon=True)]
    for th in ths:
        th.start()
    until = time.monotonic() + timeout
    while p.poll() is None:
        if state.cancel or time.monotonic() > until:
            p.kill()
            p.wait()
            return None, "".join(out), "".join(err)
        time.sleep(0.15)
    for th in ths:
        th.join(2)
    return p.returncode, "".join(out), "".join(err)


def _why(err):
    """What went wrong, from rclone's messages, without its dates and the words around it."""
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    ends = [ln for ln in lines if re.search(r"Fatal error|ERROR|CRITICAL|^Error:", ln)] or lines
    msg = re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s+", "", ends[-1]) if ends else "rclone stopped without saying why."
    msg = re.sub(r"^(NOTICE: )?(Fatal error|ERROR|CRITICAL|Error):?\s*", "", msg)
    return msg[:300]


def _answer(opt, shared):
    """Rclone's question answered for you (a string), or None when it's one for you."""
    name = opt.get("Name")
    if name == "config_is_local":
        return "true"                 # sign in with the browser on this computer
    if name == "config_shared_client_id":
        return "true" if shared else "false"
    d = opt.get("Default")
    if isinstance(d, bool):
        return "true" if d else "false"
    if d not in (None, ""):
        return str(d)
    return None


def connect(a, state):
    """One go at connecting: {service, fields, password, shared} to begin, then {name, step, result} with an answer.
    Gives {done, name, path}, {ask: a question for you, name, step}, {error} or {cancelled}."""
    with _lock:
        if not exe():
            return {"error": "Atlas needs rclone to connect cloud accounts."}
        service = str(a.get("service") or "")
        name = str(a.get("name") or "")
        first = not name
        if first:
            if service not in SERVICES:
                return {"error": "Atlas doesn't know that service."}
            _ensure_config()
            taken = {x["name"] for x in accounts()}
            name, n = SERVICES[service], 1
            while name in taken:
                n += 1
                name = "%s %d" % (SERVICES[service], n)
        base = _base()

    def said(ln):
        m = re.search(r"https?://127\.0\.0\.1:\d+/auth\S*", ln)
        if m:
            state.events.put({"url": m.group(0)})

    if first:
        cmd = base + ["config", "create", name, service, "--non-interactive", "--no-obscure"]
        fields = a.get("fields") or {}
        for k in FIELDS.get(service, ()):
            if fields.get(k):
                cmd += [k, str(fields[k])]
        if a.get("password"):
            r = subprocess.run([exe(), "obscure", "-"], input=str(a["password"]), capture_output=True, text=True, timeout=30)
            cmd += ["password", r.stdout.strip()]
    else:
        cmd = base + ["config", "update", name, "--non-interactive", "--continue", "--state", str(a.get("step") or ""), "--result", str(a.get("result") or "")]
    shared = bool(a.get("shared"))
    for _ in range(40):
        code, out, err = _run(cmd, state, SIGN_IN_WAIT, said)
        if code is None:
            _abandon(name)
            return {"cancelled": True} if state.cancel else {"error": "Signing in took too long. Try again."}
        if code != 0:
            _abandon(name)
            return {"error": _why(err)}
        try:
            ans = json.loads(out[out.index("{"):])
        except ValueError:
            _abandon(name)
            return {"error": _why(err or out)}
        if ans.get("Error"):
            _abandon(name)
            return {"error": str(ans["Error"])[:300]}
        opt = ans.get("Option")
        if not opt:
            return _connected(name, service or a.get("service"))
        said_for_you = _answer(opt, shared)
        if said_for_you is None:
            return {"ask": {"id": opt.get("Name"), "help": str(opt.get("Help") or "")[:600], "password": bool(opt.get("IsPassword"))}, "name": name, "step": ans.get("State"), "service": service}
        cmd = base + ["config", "update", name, "--non-interactive", "--continue", "--state", ans.get("State") or "", "--result", said_for_you]
    _abandon(name)
    return {"error": "Rclone kept asking questions Atlas couldn't answer."}


def _abandon(name):
    try:
        subprocess.run(_base() + ["config", "delete", name], stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    if not accounts():
        _forget_config()


def abandon(name):
    """A sign-in you stopped halfway (at Apple's code, say). An account already connected isn't touched."""
    if not any(x["name"] == name for x in accounts()):
        _abandon(name)
    return True


def _connected(name, service):
    with _lock:
        items = [x for x in accounts() if x["name"] != name] + [{"name": name, "service": service}]
        _save_accounts(items)
    problem = mount(name)
    return {"done": True, "name": name, "path": os.path.join(MOUNTS, name), "problem": problem}


# ---- The folders --------------------------------------------------------------------------------------------
def mounts():
    """What rclone has mounted now: folder -> its remote."""
    found = {}
    try:
        with open("/proc/self/mounts") as f:
            for ln in f:
                parts = ln.split()
                if len(parts) >= 3 and parts[2] == "fuse.rclone":
                    target = parts[1].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
                    found[target] = parts[0].replace("\\040", " ")
    except OSError:
        pass
    return found


def _unmount(folder, force=False):
    tool = shutil.which("fusermount3") or shutil.which("fusermount") or "fusermount3"
    return subprocess.run([tool, "-uz" if force else "-u", folder], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)


def mount(name):
    """Shows an account at ~/Cloud/<name>. It stays there after Atlas closes, for other apps. The problem, or ""."""
    folder = os.path.join(MOUNTS, name)
    if folder in mounts():
        try:
            os.stat(folder)
            _problems.pop(name, None)
            return ""
        except OSError:
            _unmount(folder, True)     # left behind by an rclone that ended
    try:
        os.makedirs(folder, exist_ok=True)
        os.makedirs(CACHE, exist_ok=True)
        cmd = _base() + ["mount", name + ":", folder, "--vfs-cache-mode", "full", "--vfs-cache-max-size", "5G", "--vfs-cache-max-age", "168h",
                         "--dir-cache-time", "5m", "--cache-dir", CACHE, "--daemon", "--daemon-wait", "30s", "--log-file", LOG, "--log-level", "NOTICE"]
        # no pipes: the rclone left running would hold them, and this would wait for it
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45, cwd="/")
    except (OSError, subprocess.SubprocessError) as e:
        _problems[name] = str(e)
        return _problems[name]
    if r.returncode or folder not in mounts():
        _problems[name] = "Couldn't reach it. It may be offline, or need signing in again."
        return _problems[name]
    _problems.pop(name, None)
    return ""


def mount_all():
    if exe():
        for x in accounts():
            mount(x["name"])


def _uploading(name):
    """Whether any of an account's files are still waiting to go up."""
    top = os.path.join(CACHE, "vfsMeta", name)
    for folder, _dirs, files in os.walk(top):
        for fn in files:
            try:
                with open(os.path.join(folder, fn), encoding="utf-8") as f:
                    if json.load(f).get("Dirty"):
                        return True
            except (OSError, ValueError):
                pass
    return False


def disconnect(name, force=False):
    """Takes an account off this computer. Its files stay in the cloud. {done} or {busy: "uploads" or "open"}."""
    if not any(x["name"] == name for x in accounts()):
        return {"error": "Atlas doesn't have that account."}
    folder = os.path.join(MOUNTS, name)
    if _uploading(name):
        return {"busy": "uploads"}
    if folder in mounts():
        r = _unmount(folder, force)
        if r.returncode:
            return {"busy": "open"}
    subprocess.run(_base() + ["config", "delete", name], stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    with _lock:
        items = [x for x in accounts() if x["name"] != name]
        _save_accounts(items)
    for spot in (os.path.join(CACHE, "vfs", name), os.path.join(CACHE, "vfsMeta", name)):
        shutil.rmtree(spot, ignore_errors=True)
    try:
        os.rmdir(folder)
        os.rmdir(MOUNTS)     # only if it's empty
    except OSError:
        pass
    _problems.pop(name, None)
    if not items:
        _forget_config()
    return {"done": True}


def on_mount(path):
    """Is this one of the folders Atlas mounted? (Deleting there can't use the Trash; online_only, below.)"""
    return path == MOUNTS or path.startswith(MOUNTS + os.sep)


def online_only(path, st):
    """Whether a file under ~/Cloud is yet to be fetched: it isn't complete in the cache."""
    if not path.startswith(MOUNTS + os.sep):
        return False
    try:
        return os.stat(os.path.join(CACHE, "vfs", path[len(MOUNTS) + 1:])).st_size != st.st_size
    except OSError:
        return True


def problem(name):
    return _problems.get(name, "")


def info():
    return {"rclone": bool(exe()), "keyring": bool(shutil.which("secret-tool")), "services": SERVICES, "folder": MOUNTS}
