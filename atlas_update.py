"""
Updating Atlas from its releases on GitHub: whether there is a newer one, and putting it in place.

  Linux    the AppImage Atlas runs from ($APPIMAGE) is replaced by the new one
  Windows  the new installer runs silently over this install, and opens Atlas again
  macOS    the new Atlas.app is copied out of its disk image and swapped in for this one

Each then starts the new Atlas once this one has closed. Run any other way (from the source, say),
Atlas only says there's a new version and where to get it.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

REPO = "kanibusrex/atlas-linux"


def _tls():
    """Secure connections checked against the system's own trusted certificates (macOS's Keychain, Windows'
    store, Linux's), through truststore: the Python inside the built app has no list of its own to go by."""
    try:
        import ssl
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return None


def _open(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout, context=_tls())


LATEST = "https://api.github.com/repos/" + REPO + "/releases/latest"
ASSET = {"linux": re.compile(r"^Atlas-x86_64\.AppImage$"),
         "windows": re.compile(r"^Atlas-[\d.]+-windows-x64-setup\.exe$"),
         "mac": re.compile(r"^Atlas-[\d.]+-mac-arm64\.dmg$")}


def version_tuple(v):
    return tuple(int(n) for n in re.findall(r"\d+", v)[:3])


def latest(current):
    """The newest release: its version, notes, page, and the download for each system."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Atlas/" + current}
    if os.environ.get("GITHUB_TOKEN"):   # in CI, whose machines share the API's hourly allowance
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    try:
        with _open(urllib.request.Request(LATEST, headers=headers), 15) as r:
            rel = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code not in (403, 429):
            raise
        return _latest_by_page(current)   # the API's allowance for this network is used up for the hour
    return {"version": str(rel.get("tag_name") or "").lstrip("v"), "notes": str(rel.get("body") or ""), "url": rel.get("html_url") or "",
            "assets": {a["name"]: {"url": a["browser_download_url"], "size": a.get("size") or 0} for a in rel.get("assets") or []}}


def _latest_by_page(current):
    """The newest release without the API: its page's address names the version, and each download's name
    follows from it. No notes this way."""
    page = "https://github.com/" + REPO + "/releases/latest"
    with _open(urllib.request.Request(page, method="HEAD", headers={"User-Agent": "Atlas/" + current}), 15) as r:
        url = r.geturl()   # .../releases/tag/v1.2.1, once the redirect is followed
    tag = url.rstrip("/").rsplit("/", 1)[-1]
    v = tag.lstrip("v")
    if not re.match(r"^\d+\.\d+", v):
        raise RuntimeError("GitHub didn't say which version is the newest.")
    names = ["Atlas-x86_64.AppImage", "Atlas-%s-windows-x64-setup.exe" % v, "Atlas-%s-mac-arm64.dmg" % v]
    base = "https://github.com/" + REPO + "/releases/download/" + tag + "/"
    return {"version": v, "notes": "", "url": url, "assets": {n: {"url": base + n, "size": 0} for n in names}}


def asset_for(system, rel):
    for name, a in rel["assets"].items():
        if ASSET[system].match(name):
            return dict(a, name=name)
    return None


# ---- Where this Atlas lives, so the new one can take its place ------------------------------------------
def target(system):
    """What gets replaced: the AppImage, the installed app (Windows), or the app bundle. None when run from the source."""
    if system == "linux":
        p = os.environ.get("APPIMAGE") or ""
        return p if p and os.access(os.path.dirname(p), os.W_OK) else None
    if not getattr(sys, "frozen", False):
        return None
    if system == "windows":
        return os.path.dirname(sys.executable)
    bundle = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
    return bundle if bundle.endswith(".app") and os.access(os.path.dirname(bundle), os.W_OK) else None


def download(a, dest_dir, progress):
    """Fetches a download, telling progress(done, total) as it goes. Returns the file."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, a["name"])
    req = urllib.request.Request(a["url"], headers={"User-Agent": "Atlas"})
    with _open(req, 60) as r, open(dest + ".part", "wb") as f:
        total, done = int(r.headers.get("Content-Length") or a.get("size") or 0), 0
        while True:
            buf = r.read(262144)
            if not buf:
                break
            f.write(buf)
            done += len(buf)
            progress(done, total)
    if total and done != total:
        raise RuntimeError("The download stopped part way. Try again.")
    os.replace(dest + ".part", dest)
    return dest


# ---- Putting it in place, and starting it once this Atlas has closed -------------------------------------
def install(system, file, where):
    """Puts the downloaded update in place, and leaves something waiting to start it. Atlas closes after.
    On Windows, gives back the installer, still running (its log is the download's name + .log)."""
    me = os.getpid()
    if system == "linux":
        new = where + ".new"
        shutil.copyfile(file, new)
        os.chmod(new, 0o755)
        os.replace(new, where)   # a running Atlas keeps the old one open; anything started now gets the new one
        _after(me, [where])
    elif system == "windows":
        # silently over this install; the installer opens Atlas again when it's done (see windows/atlas.iss)
        return subprocess.Popen([file, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/LOG=" + file + ".log"],
                                creationflags=0x00000008 | 0x00000200)   # detached, in its own group, so it outlives Atlas
    else:
        mnt = tempfile.mkdtemp(prefix="atlas-update-")
        subprocess.run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mnt, file], check=True,
                       capture_output=True, timeout=120)
        try:
            new = os.path.join(os.path.dirname(where), ".Atlas-update.app")
            shutil.rmtree(new, ignore_errors=True)
            subprocess.run(["/usr/bin/ditto", os.path.join(mnt, "Atlas.app"), new], check=True, capture_output=True, timeout=300)
        finally:
            subprocess.run(["/usr/bin/hdiutil", "detach", mnt, "-quiet"], capture_output=True, timeout=60)
        # the running Atlas keeps working from its old bundle, moved aside; the new one takes its name
        old = os.path.join(os.path.dirname(where), ".Atlas-old.app")
        shutil.rmtree(old, ignore_errors=True)
        os.rename(where, old)
        os.rename(new, where)
        _after(me, ["/usr/bin/open", "-n", where], cleanup=old)


def _after(pid, cmd, cleanup=None):
    """Once Atlas (pid) has closed: removes what was left behind, and starts cmd."""
    script = 'while kill -0 %d 2>/dev/null; do sleep 0.3; done; %s exec "$@"' % (
        pid, ('rm -rf "%s";' % cleanup.replace('"', '\\"')) if cleanup else "")
    subprocess.Popen(["/bin/sh", "-c", script, "atlas-update"] + cmd, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
