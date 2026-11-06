"""
Doing what Atlas isn't allowed to as you, as the administrator (Linux): pkexec runs the system's own
tools (rm, mv, cp, mkdir, touch, chmod) with the files as their arguments; no script of Atlas's own runs as
root, and nothing is pieced together into a command line (the paths go to sh as "$@").

pkexec asks for the password itself: through the desktop's polkit agent when one is running (a window of
the desktop's), otherwise on its terminal. Atlas gives pkexec a terminal of its own (a pty), so when it asks
there Atlas can pass on the password typed into Atlas's own dialog. The password is never kept.
"""

import os
import pty
import select
import shutil
import signal
import time

# what each action runs: sh -c SCRIPT sh ARGS... (the first argument after the script is $0)
SCRIPTS = {
    # paths...
    "delete": 'exec rm -rf -- "$@"',
    # pairs: from to from to ...
    "rename": 'while [ $# -ge 2 ]; do mv -n -T -- "$1" "$2" || exit 1; shift 2; done',
    "mkdir": 'exec mkdir -- "$@"',
    "newfile": 'set -C; for f; do : > "$f" || exit 1; done',
    # the folder, then what goes into it
    "copy": 'd=$1; shift; exec cp -a -n -- "$@" "$d"',
    "move": 'd=$1; shift; exec mv -n -- "$@" "$d"',
    # on off (octal) recursive(0/1), then paths: chmod's own +/- forms
    "chmod": 'on=$1; off=$2; r=$3; shift 3; o=""; [ "$r" = 1 ] && o=-R; '
             '[ "$on" != 0 ] && { chmod $o "+$on" -- "$@" || exit 1; }; [ "$off" != 0 ] && { chmod $o "-$off" -- "$@" || exit 1; }; exit 0',
}
RUN_WAIT = 3600      # seconds: a desktop's own password window can sit there as long as the person wants


def available():
    return bool(shutil.which("pkexec"))


def args_for(action, m):
    """The arguments for an action's script, from the page's request."""
    if action == "delete":
        return [str(p) for p in m.get("paths") or []]
    if action == "rename":
        out = []
        for it in m.get("items") or []:
            src, name = str(it.get("path") or ""), str(it.get("name") or "")
            if not src or not name or "/" in name or name in (".", ".."):
                raise ValueError("That name is not allowed.")
            out += [src, os.path.join(os.path.dirname(src.rstrip("/")) or "/", name)]
        return out
    if action in ("mkdir", "newfile"):
        return [str(p) for p in m.get("paths") or []]
    if action in ("copy", "move"):
        return [str(m.get("dest") or "")] + [str(p) for p in m.get("paths") or []]
    if action == "chmod":
        return ["%o" % (int(m.get("on") or 0) & 0o7777), "%o" % (int(m.get("off") or 0) & 0o7777), "1" if m.get("recursive") else "0"] + \
            [str(p) for p in m.get("paths") or []]
    raise ValueError("Atlas can't do that as the administrator.")


def run(action, m, password=None):
    """{"ok": True}, {"needPassword": True} (pkexec asked on its terminal, and no password was given),
    {"wrongPassword": True}, {"cancelled": True}, or {"error": "..."}."""
    if action not in SCRIPTS:
        return {"error": "Atlas can't do that as the administrator."}
    args = args_for(action, m)
    if len(args) < 1 or any(not a for a in args):
        return {"error": "There is nothing to do."}
    exe = shutil.which("pkexec")
    if not exe:
        return {"error": "pkexec isn't installed (it comes with polkit: sudo pacman -S polkit)."}
    argv = [exe, "/bin/sh", "-c", SCRIPTS[action], "sh"] + args
    pid, fd = pty.fork()
    if pid == 0:   # the child: pkexec on the pty
        try:
            os.execv(exe, argv)
        finally:
            os._exit(127)
    out, asked, sent = b"", 0, False
    start = time.monotonic()
    try:
        while True:
            wait = RUN_WAIT - (time.monotonic() - start)
            if wait <= 0:
                os.kill(pid, signal.SIGTERM)
                return {"error": "It took too long."}
            r, _, _ = select.select([fd], [], [], min(wait, 1.0))
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break   # the other end closed: pkexec is done
            if not chunk:
                break
            out += chunk
            tail = out[-200:].lower()
            if tail.rstrip().endswith(b"password:"):
                asked += 1
                if asked > 1:
                    # asked again: the password given wasn't right
                    os.kill(pid, signal.SIGTERM)
                    return {"wrongPassword": True}
                if password is None:
                    os.kill(pid, signal.SIGTERM)
                    return {"needPassword": True}
                os.write(fd, password.encode() + b"\n")
                sent, start = True, time.monotonic()
                out = b""   # what came before (the prompt) isn't the command's to tell
    finally:
        password = None
        try:
            os.close(fd)
        except OSError:
            pass
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    text = out.decode("utf-8", "replace").replace("\r", "")
    if code == 0:
        return {"ok": True}
    low = text.lower()
    if "authentication failed" in low or "sorry, try again" in low:
        return {"wrongPassword": True}
    if code == 126 or "dismissed" in low:
        return {"cancelled": True}
    if code == 127 and "not authorized" in low:
        return {"error": "This account isn't allowed to act as the administrator."}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("====")]
    return {"error": (lines[-1] if lines else "It didn't work (code %d)." % code)}
