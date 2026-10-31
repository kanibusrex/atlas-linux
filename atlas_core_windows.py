"""
atlas_core_windows.py: the file work that is Windows' own: drive letters, the Recycle Bin, hidden files,
the names Windows won't take, and your Documents and Downloads folders. atlas_core.py takes from here
what it can't do the same way everywhere; atlas_core_linux.py gives the same names on Linux.

The service-desk requests (who has access to a folder, the members of a group, mapping drives, a
server's shares, what's using a file) are answered by AtlasCore.ps1, the Windows Atlas's own file
work, run in PowerShell one request at a time: Windows has the tools for those in .NET and COM.

Like atlas_core.py, no windows here, only files: everything can run in a background thread.
"""

import base64
import ctypes
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import uuid
import winreg
from ctypes import wintypes

import atlas_core as core

HERE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))   # beside it, or in the built app
PATH_HINT = "C:\\Temp, %APPDATA% or \\\\server\\share"
ELSEWHERE = "Linux"   # where the requests in NOT_HERE work

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetLogicalDrives.restype = wintypes.DWORD
k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
k32.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
k32.GetDiskFreeSpaceExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_void_p]
k32.SetThreadErrorMode.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
k32.LocalFree.argtypes = [ctypes.c_void_p]

# what System.IO.DriveType calls each kind of drive, as the page expects
DRIVE_TYPES = {0: "Unknown", 1: "NoRootDirectory", 2: "Removable", 3: "Fixed", 4: "Network", 5: "CDRom", 6: "Ram"}
FILE_ATTRIBUTE_HIDDEN = 0x2
ATTRIBUTES = ((0x1, "ReadOnly"), (0x2, "Hidden"), (0x4, "System"), (0x10, "Directory"), (0x20, "Archive"), (0x80, "Normal"),
              (0x100, "Temporary"), (0x200, "SparseFile"), (0x400, "ReparsePoint"), (0x800, "Compressed"), (0x1000, "Offline"),
              (0x2000, "NotContentIndexed"), (0x4000, "Encrypted"))


# ---- Your own folders: Documents, Downloads... (Windows' known folders, wherever OneDrive moved them) ---
KNOWN = {"DESKTOP": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "DOCUMENTS": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
         "DOWNLOAD": "374DE290-123F-4565-9164-39C4925E467B", "PICTURES": "33E28130-4E1E-4676-835A-98395C3BC3BB",
         "MUSIC": "4BD8D571-6D19-48D3-BE97-422220080E43", "VIDEOS": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
         "TEMPLATES": "A63293E8-664E-48DB-A079-DF759E0509F7"}
NAMES = {"DESKTOP": "Desktop", "DOCUMENTS": "Documents", "DOWNLOAD": "Downloads", "PICTURES": "Pictures",
         "MUSIC": "Music", "VIDEOS": "Videos", "TEMPLATES": "Templates"}


def user_dir(key):
    guid = KNOWN.get(key)
    if guid:
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.c_char_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        out = ctypes.c_wchar_p()
        if shell32.SHGetKnownFolderPath(uuid.UUID(guid).bytes_le, 0, None, ctypes.byref(out)) == 0:
            try:
                return out.value
            finally:
                ole32.CoTaskMemFree(out)
    return os.path.join(os.path.expanduser("~"), NAMES.get(key, key.title()))


# ---- Drives and the usual places ----------------------------------------------------------------
def _volume(root):
    """A drive's label and space. Nothing when it has no disc in it, or (a network drive) its server is gone."""
    box = {}

    def go():
        old = wintypes.DWORD()
        k32.SetThreadErrorMode(1, ctypes.byref(old))   # no "insert a disk" box for an empty card reader
        label = ctypes.create_unicode_buffer(261)
        if k32.GetVolumeInformationW(root, label, 261, None, None, None, None, 0):
            free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
            if k32.GetDiskFreeSpaceExW(root, ctypes.byref(free), ctypes.byref(total), None):
                box["v"] = (label.value, total.value, free.value)
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(2.0)
    return box.get("v")


def _mapped(letter):
    """A mapped drive's own address (\\\\server\\share), and whether it comes back at sign-in."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Network\\" + letter) as k:
            return str(winreg.QueryValueEx(k, "RemotePath")[0]), True
    except OSError:
        pass
    mpr = ctypes.WinDLL("mpr")
    buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
    if mpr.WNetGetConnectionW(letter + ":", buf, ctypes.byref(n)) == 0:
        return buf.value, False
    return "", False


def drive_letters():
    mask = k32.GetLogicalDrives()
    return [chr(65 + i) for i in range(26) if mask & (1 << i)]


def drives():
    out = []
    for letter in drive_letters():
        root = letter + ":\\"
        info = {"path": root, "type": DRIVE_TYPES.get(k32.GetDriveTypeW(root), "Unknown"), "ready": False, "label": "",
                "total": 0, "free": 0, "unc": ""}
        v = _volume(root)
        if v:
            info["label"], info["total"], info["free"] = v
            info["ready"] = True
        if info["type"] == "Network":
            info["unc"], reconnect = _mapped(letter)
            if reconnect:
                info["reconnect"] = True
        out.append(info)
    home = os.path.expanduser("~")
    places = []
    for name, p in (("Desktop", user_dir("DESKTOP")), ("Documents", user_dir("DOCUMENTS")), ("Downloads", user_dir("DOWNLOAD")),
                    ("Pictures", user_dir("PICTURES")), ("Home", home),
                    ("OneDrive", os.environ.get("OneDriveCommercial") or os.environ.get("OneDrive") or "")):
        if p and os.path.isdir(p):
            places.append({"name": name, "path": p})
    return {"drives": out, "places": places, "user": os.environ.get("USERNAME", ""), "machine": os.environ.get("COMPUTERNAME", "")}


# ---- A folder's contents: Windows' own ways of writing a path ------------------------------------------
_SERVER = re.compile(r"^\\\\[^\\]+\\?$")
_SHARE_TOP = re.compile(r"^(\\\\[^\\]+)\\[^\\]+\\?$")


def list_dir(path):
    p = core.resolve_path(path)
    if re.match(r"^[A-Za-z]:$", p):
        p += "\\"
    m = re.search(r"%[^%\\]+%", p)
    if m:
        return {"path": p, "error": "Windows has no setting called " + m.group(0) + ", so that folder is not known."}
    # a server on its own (\\server) lists its shares
    if _SERVER.match(p):
        return powershell("Get-FDShares ([string]$A.server)", {"server": p})
    r = core.list_dir(p)
    # the top of a share goes up to its server
    m = _SHARE_TOP.match(r.get("path") or "")
    if m and not r.get("error") and not r.get("parent"):
        r["parent"] = m.group(1)
    return r


def details(path):
    r = core.details(path)
    st = os.lstat(path)
    a = getattr(st, "st_file_attributes", 0)
    r["attributes"] = ", ".join(n for bit, n in ATTRIBUTES if a & bit) or "Normal"
    r["owner"] = owner_of(path)
    return r


def owner_of(path):
    """Who owns a file or folder (DOMAIN\\name), from its security descriptor."""
    adv = ctypes.WinDLL("advapi32")
    adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                                      wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    sid, sd = ctypes.c_void_p(), ctypes.c_void_p()
    if adv.GetNamedSecurityInfoW(path, 1, 1, ctypes.byref(sid), None, None, None, ctypes.byref(sd)) != 0:   # a file, its owner
        return ""
    try:
        name, dom = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        n, d, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
        if not adv.LookupAccountSidW(None, sid, name, ctypes.byref(n), dom, ctypes.byref(d), ctypes.byref(use)):
            return ""
        return dom.value + "\\" + name.value if dom.value else name.value
    finally:
        k32.LocalFree(sd)


# ---- Deleting: to the Recycle Bin, as Explorer does -------------------------------------------------------
class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR)]


def recyclable(path):
    """Only fixed and removable drives have a Recycle Bin; network shares don't."""
    if path.startswith("\\\\"):
        return False
    root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    return k32.GetDriveTypeW(root) in (2, 3)


def recycle(p):
    # a list of paths, ended by two nulls (a str with a null inside can't be passed as a plain string)
    names = ctypes.create_unicode_buffer(os.path.abspath(p) + "\0")
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3                                   # delete
    op.pFrom = ctypes.addressof(names)
    op.fFlags = 0x40 | 0x10 | 0x4 | 0x400          # to the Recycle Bin, no questions, no progress, no error boxes
    r = ctypes.WinDLL("shell32").SHFileOperationW(ctypes.byref(op))
    if r != 0 or op.fAnyOperationsAborted:
        raise OSError("Windows said no (error 0x%X)" % r)


def _writable(fn, p, _exc):
    """A read-only file in a folder being deleted: made writable, then deleted after all."""
    os.chmod(p, stat.S_IWRITE)
    fn(p)


def remove_tree(p):
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p, onexc=_writable)
    else:
        try:
            os.unlink(p)
        except PermissionError:
            os.chmod(p, stat.S_IWRITE)
            os.unlink(p)


def remove_items(paths, permanent, state):
    removed, problems = 0, []
    for p in paths or []:
        if state and state.cancel:
            break
        p = str(p)
        try:
            if not core.exists(p):
                continue
            if not permanent:
                if not recyclable(p):
                    problems.append({"path": p, "error": "This has no Recycle Bin, so it was not deleted."})
                    continue
                if len(p) >= 260:
                    problems.append({"path": p, "error": "Its path is too long for the Recycle Bin. Delete it permanently instead (Shift+Delete)."})
                    continue
                recycle(p)
            else:
                remove_tree(p)
            removed += 1
        except OSError as e:
            problems.append({"path": p, "error": e.strerror or str(e)})
    return {"removed": removed, "problems": problems}


# ---- The service-desk requests, answered by AtlasCore.ps1 -------------------------------------------------
def powershell(call, a):
    """Runs one of AtlasCore.ps1's functions, given the request's arguments as $A, and returns its answer."""
    script = ("$ErrorActionPreference = 'Stop'\n"
              "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
              ". '" + os.path.join(HERE, "AtlasCore.ps1").replace("'", "''") + "'\n"
              "$A = '" + json.dumps(a).replace("'", "''") + "' | ConvertFrom-Json\n"
              "try { $r = " + call + "; ConvertTo-FDJson $r } catch { ConvertTo-FDJson @{ __error = $_.Exception.Message } }\n")
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode()]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180, stdin=subprocess.DEVNULL, creationflags=0x08000000)   # no console window
    except subprocess.TimeoutExpired:
        raise RuntimeError("Windows took too long to answer.")
    lines = [ln for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
    if not lines:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(err[0] if err else "PowerShell gave no answer.")
    res = json.loads(lines[-1])
    if isinstance(res, dict) and res.get("__error"):
        raise RuntimeError(res["__error"])
    return res


# ---- What atlas_core.py asks of every system --------------------------------------------------------
def is_hidden(_name, st):
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_HIDDEN)


def hidden_entry(de):
    try:
        return is_hidden(de.name, de.stat(follow_symlinks=False))
    except OSError:
        return False


def owner(_uid):
    return ""


def bad_name(n):
    if any(c in n for c in '<>:"/\\|?*') or any(ord(c) < 32 for c in n):
        return 'A name cannot contain any of these: \\ / : * ? " < > |'
    if n.endswith((".", " ")):
        return "A name cannot end with a dot or a space."
    if re.match(r"^(CON|PRN|AUX|NUL|COM\d|LPT\d)(\..*)?$", n, re.I):
        return "Windows keeps the name " + n + " for itself."
    return ""


def listing(_full, out):
    return out


# requests answered here, each given the page's arguments and the request's State
OPS = {
    "list": lambda a, state: list_dir(str(a.get("path") or "")),
    "drives": lambda a, state: drives(),
    "details": lambda a, state: details(str(a.get("path"))),
    "delete": lambda a, state: remove_items(a.get("paths"), bool(a.get("permanent")), state),
    "recyclable": lambda a, state: {"ok": recyclable(str(a.get("path")))},
    "shares": lambda a, state: powershell("Get-FDShares ([string]$A.server)", a),
    "inuse": lambda a, state: powershell("Get-FDUsers @($A.paths)", a),
    "access": lambda a, state: powershell("Get-FDAccess ([string]$A.path)", a),
    "members": lambda a, state: powershell("Get-FDGroupMembers ([string]$A.name)", a),
    "letters": lambda a, state: powershell("Get-FDDriveLetters", a),
    "mapdrive": lambda a, state: powershell("Connect-FDDrive ([string]$A.letter) ([string]$A.path) ([bool]$A.reconnect)", a),
    "unmapdrive": lambda a, state: powershell("Disconnect-FDDrive ([string]$A.letter) ([bool]$A.force)", a),
}

# Linux's own: its Trash, Samba's smbclient, permission bits, ending a program, Omarchy's wallpaper
NOT_HERE = {"restore", "emptytrash", "smbshares", "perms", "chperm", "endprogram", "wallpaper"}
