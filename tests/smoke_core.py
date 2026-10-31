"""
A quick check of Atlas's file work on the system it runs on: python tests/smoke_core.py
Makes a small folder in the temp folder, sends it the page's requests (list, search, copy, zip, delete...)
and checks the answers. Prints what each system-specific request gave, for reading in a CI log.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import atlas_core as core  # noqa: E402

failures = []


def run(op, **a):
    return core.run(op, a, core.State())


def check(what, ok, got=None):
    print(("ok    " if ok else "FAIL  ") + what + ("" if ok else "  ->  " + json.dumps(got, default=str)[:400]))
    if not ok:
        failures.append(what)


def show(what, fn):
    try:
        r = fn()
        print("info  " + what + ": " + json.dumps(r, default=str)[:600])
    except Exception as e:
        print("info  " + what + " raised: " + str(e)[:300])


# on Linux not in /tmp: GIO won't move things from a RAM disk to the Trash
box = tempfile.mkdtemp(prefix="atlas-smoke-", dir=None if sys.platform == "win32" else os.path.expanduser("~/.cache"))
try:
    a, b = os.path.join(box, "a"), os.path.join(box, "b")
    os.makedirs(os.path.join(a, "sub", "deep"))
    os.makedirs(b)
    for rel, text in (("one.txt", "hello"), ("sub/two.md", "# hi\n"), ("sub/deep/three.txt", "x" * 1000)):
        with open(os.path.join(a, *rel.split("/")), "w", newline="") as f:   # the same bytes on Windows
            f.write(text)

    r = run("list", path=a)
    check("list a folder", sorted(e["n"] for e in r.get("entries", [])) == ["one.txt", "sub"], r)
    check("its parent", r.get("parent") == box, r)
    top = os.path.splitdrive(box)[0] + os.sep
    r = run("list", path=top)
    check("the top of the drive has no parent", r.get("parent") == "" and not r.get("error"), r)
    r = run("list", path="relative")
    check("a path that isn't full is refused", bool(r.get("error")), r)

    r = run("search", root=a, query="two", options={})
    check("search", r.get("found") == 1, r)
    r = run("sizes", paths=[a])
    check("folder size", r["sizes"][0]["bytes"] == 1010 and r["sizes"][0]["files"] == 3, r)

    r = run("mkdir", dir=b, name="new")
    check("new folder", r.get("name") == "new" and os.path.isdir(os.path.join(b, "new")), r)
    r = run("mkdir", dir=b, name="new")
    check("a taken name gets a free one", r.get("name") == "new (2)", r)
    r = run("mkdir", dir=b, name="a/b")
    check("a name with / is refused", bool(r.get("error")), r)
    if sys.platform == "win32":
        r = run("mkdir", dir=b, name="CON")
        check("a name Windows keeps is refused", bool(r.get("error")), r)
        r = run("mkdir", dir=b, name="what?")
        check("a name with ? is refused", bool(r.get("error")), r)

    r = run("copy", sources=[a], dest=b, move=False, policy="keep")
    check("copy a folder", r.get("filesDone") == 3 and not r.get("problems"), r)
    r = run("copy", sources=[os.path.join(b, "a")], dest=box, move=True, policy="keep")
    check("move it where a folder of that name is", not r.get("problems") and os.path.isdir(os.path.join(box, "a (2)")), r)
    r = run("rename", items=[{"path": os.path.join(box, "a (2)"), "name": "c"}])
    check("rename", r.get("done") == 1 and os.path.isdir(os.path.join(box, "c")), r)

    r = run("hash", path=os.path.join(a, "one.txt"))
    check("hash", r.get("sha256", "").startswith("2CF24DBA"), r)
    r = run("compare", left=a, right=os.path.join(box, "c"))
    check("compare two copies", r["counts"]["same"] == 3 and not r["items"], r)

    r = run("zip", paths=[os.path.join(a, "sub")], dir=box, name="s.zip")
    check("make a zip", r.get("files") == 2, r)
    z = os.path.join(box, "s.zip")
    r = run("list", path=os.path.join(z, "sub"))
    check("look inside it", sorted(e["n"] for e in r.get("entries", [])) == ["deep", "two.md"], r)
    r = run("unzip", zip=z, dest="", entries=None, inner="")
    check("extract it", r.get("files") == 2 and not r.get("problems"), r)
    r = run("readtext", path=os.path.join(a, "sub", "two.md"))
    check("read a text file", r.get("text") == "# hi\n", r)
    r = run("details", path=os.path.join(a, "one.txt"))
    check("details", r.get("s") == 5, r)
    print("info  owner: " + repr(r.get("owner")) + ", attributes: " + repr(r.get("attributes")))

    r = run("drives")
    check("drives", len(r.get("drives", [])) > 0 and len(r.get("places", [])) > 0, r)
    print("info  drives: " + ", ".join("%s (%s, %s)" % (d["path"], d["type"], d.get("label")) for d in r["drives"]))
    print("info  places: " + ", ".join(p["name"] for p in r["places"]))
    r = run("recyclable", path=box)
    print("info  recyclable: " + json.dumps(r))

    gone = os.path.join(box, "c", "one.txt")
    r = run("delete", paths=[gone], permanent=False)
    check("delete to the bin", r.get("removed") == 1 and not os.path.exists(gone), r)
    if r.get("trashed"):
        run("restore", paths=[t["to"] for t in r["trashed"]])   # out of the Trash again, so it isn't left there
        run("delete", paths=[gone], permanent=True)
    r = run("delete", paths=[os.path.join(box, "c")], permanent=True)
    check("delete for good", r.get("removed") == 1 and not os.path.exists(os.path.join(box, "c")), r)

    if sys.platform == "win32":
        r = run("list", path=top[:2])
        check("C: is read as C:\\", r.get("path", "").endswith(":\\") and not r.get("error"), r)
        r = run("list", path="%NOSUCHNAME%\\x")
        check("an unknown %NAME% says so", "no setting" in r.get("error", ""), r)
        show("letters (AtlasCore.ps1)", lambda: run("letters"))
        show("access (AtlasCore.ps1)", lambda: {k: v for k, v in run("access", path=box).items() if k != "entries"})
        with open(os.path.join(a, "one.txt")) as held:
            show("inuse (AtlasCore.ps1)", lambda: run("inuse", paths=[os.path.join(a, "one.txt")]))
        show("shares of \\\\localhost (AtlasCore.ps1)", lambda: run("list", path="\\\\localhost"))
finally:
    shutil.rmtree(box, ignore_errors=True)

print()
print("%d failed" % len(failures) if failures else "all passed")
sys.exit(1 if failures else 0)
