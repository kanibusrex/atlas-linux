"""
Searching inside files, for atlas_core.search when "Inside files" is on.

Plain text (notes, code, logs, CSV, HTML...) is read as it is; documents keep their words packed away, so
they are opened first: Word, Excel, PowerPoint, OpenDocument and EPUB files are zip files of XML, and a PDF's
text comes from pdftotext (Poppler) when that is installed. Letter case doesn't matter.

ripgrep (rg), when installed, reads the plain text far faster than Python can, so it does that part and
Python does only the documents; without it, Python reads everything.
"""

import html
import json
import os
import re
import shutil
import subprocess
import threading
import zipfile

TEXT_MAX = 64 << 20      # bigger plain files are skipped (a log that size is better searched in a terminal)
DOC_MAX = 200 << 20
DOCS = {".pdf", ".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".odt", ".ods", ".odp", ".epub"}
_TAG = re.compile(r"<[^>]+>")
# which parts of a zipped document hold its words
_PARTS = {
    ".docx": re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$"),
    ".docm": re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$"),
    ".xlsx": re.compile(r"^xl/(sharedStrings|worksheets/sheet\d+)\.xml$"),
    ".xlsm": re.compile(r"^xl/(sharedStrings|worksheets/sheet\d+)\.xml$"),
    ".pptx": re.compile(r"^ppt/(slides/slide\d+|notesSlides/notesSlide\d+)\.xml$"),
    ".odt": re.compile(r"^content\.xml$"), ".ods": re.compile(r"^content\.xml$"), ".odp": re.compile(r"^content\.xml$"),
    ".epub": re.compile(r"\.x?html?$", re.I),
}


def rg_path():
    return shutil.which("rg")


def pattern(query):
    """The words of query in order, with any run of spaces or line ends between them, any letter case."""
    return re.compile(r"\s+".join(re.escape(w) for w in query.split()), re.I)


def rg_pattern(query):
    # the same for ripgrep: every letter that isn't a plain one written as \x{..}, which its regex syntax always takes
    def esc(w):
        return "".join(c if c.isalnum() else "\\x{%x}" % ord(c) for c in w)
    return r"\s+".join(esc(w) for w in query.split())


def snippet(line, pat):
    """The words around a match, on one line: "...the invoice for March was..." """
    line = " ".join(line.split())
    m = pat.search(line)
    if not m:
        return line[:140]
    a, b = max(0, m.start() - 50), min(len(line), m.end() + 70)
    return ("\u2026" if a else "") + line[a:b] + ("\u2026" if b < len(line) else "")


def find_in(text, pat):
    """(line number, snippet) of the first match of pat in text, or None."""
    m = pat.search(text)
    if not m:
        return None
    start = text.rfind("\n", 0, m.start()) + 1
    end = text.find("\n", m.end())
    return text.count("\n", 0, m.start()) + 1, snippet(text[start:len(text) if end < 0 else end], pat)


def doc_text(path, ext):
    """The words in a document, or None when they can't be read here."""
    try:
        if os.path.getsize(path) > DOC_MAX:
            return None
    except OSError:
        return None
    if ext == ".pdf":
        exe = shutil.which("pdftotext")
        if not exe:
            return None
        try:
            r = subprocess.run([exe, "-q", "-enc", "UTF-8", path, "-"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    want = _PARTS.get(ext)
    if not want:
        return None
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if want.search(info.filename) and info.file_size < 64 << 20:
                    xml = z.read(info).decode("utf-8", "replace")
                    # a paragraph, row or slide's end is a line's end, so a match shows its own line
                    xml = re.sub(r"</(w:p|a:p|text:p|text:h|row|si|p|div|h\d|li|tr)>", "\n", xml)
                    out.append(html.unescape(_TAG.sub(" " if ext in (".xlsx", ".xlsm") else "", xml)))
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError, NotImplementedError):
        return None
    return "\n".join(out)


def text_file_hit(path, pat, size):
    """Where pat is in a plain text file (None when it isn't, or the file isn't text)."""
    if size > TEXT_MAX:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None   # a program, a picture, a database...: not text
    return find_in(data.decode("utf-8", "replace"), pat)


def hit_in(path, ext, pat, size):
    if ext in DOCS:
        t = doc_text(path, ext)
        return find_in(t, pat) if t else None
    return text_file_hit(path, pat, size)


def rg_text(root, query, hidden, recurse, exts, state):
    """ripgrep over the plain text below root: yields (path, line number, snippet) for each file with a match."""
    cmd = [rg_path(), "--json", "--ignore-case", "--multiline", "--no-ignore", "--no-messages", "--max-count", "1",
           "--max-filesize", str(TEXT_MAX), "--max-columns", "2000"]
    if hidden:
        cmd.append("--hidden")
    if not recurse:
        cmd += ["--max-depth", "1"]
    for x in exts:
        cmd += ["--iglob", "*" + x]
    cmd += ["--regexp", rg_pattern(query), "--", root]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    stop = threading.Event()

    def watch():
        while not stop.wait(0.2):
            if state and state.cancel:
                proc.kill()
                return
    threading.Thread(target=watch, daemon=True).start()
    pat = pattern(query)
    try:
        for raw in proc.stdout:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("type") != "match":
                continue
            d = m["data"]
            p = d["path"].get("text")
            line = (d.get("lines") or {}).get("text") or ""
            if not p:
                continue
            yield p, d.get("line_number") or 0, snippet(line, pat)
    finally:
        stop.set()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
