"""
Tries an update the way Atlas does one, against the newest release on GitHub: python tests/update_check.py
Finds it, downloads this system's file, and puts it in place, somewhere it can't hurt:
  Linux    over a copy of an AppImage in a temp folder
  Windows  runs the installer silently (it installs for the person running it, as any update would)
  macOS    swaps the release's Atlas.app in for a stand-in in a temp folder
Starting the new Atlas afterwards is left out.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import atlas_update as update  # noqa: E402

system = {"linux": "linux", "win32": "windows", "darwin": "mac"}[sys.platform]
started = []
update._after = lambda pid, cmd, cleanup=None: started.append(cmd)   # nothing is started here

# the way Atlas falls back when GitHub's API has had enough from this network for the hour
by_page = update._latest_by_page("0.0.0")
print("newest release, from its page:", by_page["version"])
rel = update.latest("0.0.0")
assert rel["version"] == by_page["version"] and update.asset_for(system, by_page)["url"] == update.asset_for(system, rel)["url"]
print("newest release:", rel["version"], "with", ", ".join(sorted(rel["assets"])))
assert update.version_tuple(rel["version"]) > update.version_tuple("1.1.0"), "1.1.0 should see it as newer"
a = update.asset_for(system, rel)
assert a, "the release has no download for " + system
seen = []
work = tempfile.mkdtemp(prefix="atlas-update-test-")
f = update.download(a, work, lambda done, total: seen.append((done, total)))
print("downloaded %s (%d bytes, %d progress reports)" % (a["name"], os.path.getsize(f), len(seen)))
assert (not a["size"] or os.path.getsize(f) == a["size"]) and seen and seen[-1][0] == seen[-1][1]

if system == "linux":
    where = os.path.join(work, "installed", "Atlas-x86_64.AppImage")
    os.makedirs(os.path.dirname(where))
    with open(where, "w") as old:
        old.write("the old one")
    update.install(system, f, where)
    assert os.path.getsize(where) == os.path.getsize(f) and os.access(where, os.X_OK)
    assert started == [[where]]
elif system == "mac":
    where = os.path.join(work, "Applications", "Atlas.app")
    os.makedirs(os.path.join(where, "Contents"))
    with open(os.path.join(where, "Contents", "old"), "w") as old:
        old.write("the old one")
    update.install(system, f, where)
    assert os.path.exists(os.path.join(where, "Contents", "MacOS", "Atlas")), "the new Atlas.app isn't in place"
    assert os.path.exists(os.path.join(work, "Applications", ".Atlas-old.app", "Contents", "old")), "the old one wasn't kept aside"
    assert started and started[0][:2] == ["/usr/bin/open", "-n"]
    print("Atlas.app swapped:", sorted(os.listdir(os.path.join(work, "Applications"))))
else:
    exe = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Atlas", "Atlas.exe")
    setup = update.install(system, f, os.path.dirname(exe))
    print("the installer finished with", setup.wait(300))
    time.sleep(2)
    if not os.path.exists(exe):
        print("---- the installer's log")
        print(open(f + ".log", encoding="utf-8", errors="replace").read() if os.path.exists(f + ".log") else "(none)")
    assert os.path.exists(exe), "the installer didn't put Atlas.exe in " + os.path.dirname(exe)
    print("installed:", exe)
    subprocess.run(["taskkill", "/F", "/IM", "Atlas.exe"], capture_output=True)   # the update opens Atlas again

shutil.rmtree(work, ignore_errors=True)
print("the update went in")
