"""
More about a file for the preview's facts: a photo's size, camera and settings, and where it was taken
(read from the file itself: JPEG and TIFF EXIF, PNG, GIF, WebP and BMP headers); a video's or song's
length, picture, sound and tags (ffprobe, which comes with ffmpeg, or Spotlight on a Mac); a PDF's pages,
title and author (pdfinfo, from Poppler, or Spotlight); an Office file's title, author and counts (its
docProps). Each answer is a list of [label, text] rows, and sometimes a "map" (latitude, longitude).
"""

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile

IMAGES = {".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
MEDIA = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".3gp",
         ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aiff", ".aif", ".alac"}
OFFICE = {".docx", ".docm", ".xlsx", ".xlsm", ".pptx"}


def facts(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in IMAGES:
            return image_facts(path, ext)
        if ext in MEDIA:
            return media_facts(path)
        if ext == ".pdf":
            return pdf_facts(path)
        if ext in OFFICE:
            return office_facts(path)
    except (OSError, ValueError, struct.error, IndexError, KeyError, zipfile.BadZipFile):
        pass
    return {"rows": []}


# ---- Pictures ---------------------------------------------------------------------------------------
def image_facts(path, ext):
    with open(path, "rb") as f:
        data = f.read(4 << 20)
    size, exif = None, None
    if data[:2] == b"\xff\xd8":
        size, exif = _jpeg(data)
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        size = struct.unpack(">II", data[16:24])
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        size = struct.unpack("<HH", data[6:10])
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        size = _webp(data)
    elif data[:2] == b"BM":
        w, h = struct.unpack("<ii", data[18:26])
        size = (w, abs(h))
    elif data[:4] in (b"II*\0", b"MM\0*"):
        exif = _tiff(data)
        if exif.get(0x0100) and exif.get(0x0101):
            size = (exif[0x0100], exif[0x0101])
    rows, out = [], {}
    if exif:
        w, h = exif.get(0xA002), exif.get(0xA003)
        if not size and w and h:
            size = (w, h)
    if size and size[0] and size[1]:
        w, h = size
        if exif and exif.get(0x0112) in (5, 6, 7, 8):
            w, h = h, w   # taken sideways: shown turned
        rows.append(["Picture", "%d × %d (%.1f megapixels)" % (w, h, w * h / 1e6) if w * h >= 1e6 else "%d × %d" % (w, h)])
    if exif:
        cam = " ".join(x for x in (_s(exif.get(0x010F)), _s(exif.get(0x0110))) if x)
        make = _s(exif.get(0x010F))
        model = _s(exif.get(0x0110))
        if make and model.lower().startswith(make.lower().split()[0]):
            cam = model   # "Canon Canon EOS R6" said once
        rows.append(["Camera", cam])
        rows.append(["Lens", _s(exif.get(0xA434))])
        taken = _s(exif.get(0x9003) or exif.get(0x0132))
        m = re.match(r"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d)", taken)
        rows.append(["Taken", "%s-%s-%s %s:%s" % m.groups() if m else ""])
        bits = []
        et = exif.get(0x829A)
        if et:
            bits.append(("1/%d s" % round(1 / et)) if et < 0.5 else ("%g s" % round(et, 1)))
        if exif.get(0x829D):
            bits.append("f/%g" % round(exif[0x829D], 1))
        if exif.get(0x8827):
            iso = exif[0x8827]
            bits.append("ISO %d" % (iso[0] if isinstance(iso, tuple) else iso))
        if exif.get(0x920A):
            bits.append("%g mm" % round(exif[0x920A], 1))
        rows.append(["Settings", ", ".join(bits)])
        if exif.get(0x9209) is not None:
            rows.append(["Flash", "Fired" if exif[0x9209] & 1 else "Didn't fire"])
        gps = exif.get("gps") or {}
        lat, lon = _deg(gps.get(2), gps.get(1)), _deg(gps.get(4), gps.get(3))
        if lat is not None and lon is not None and (lat or lon):
            rows.append(["Location", "%.5f, %.5f" % (lat, lon)])
            out["map"] = [round(lat, 6), round(lon, 6)]
    out["rows"] = [r for r in rows if r[1]]
    return out


def _s(v):
    return v.strip("\0 ").strip() if isinstance(v, str) else ""


def _deg(v, ref):
    if not isinstance(v, tuple) or len(v) < 3:
        return None
    d = v[0] + v[1] / 60 + v[2] / 3600
    return -d if _s(ref).upper() in ("S", "W") else d


def _jpeg(data):
    size, exif, i = None, None, 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        mk = data[i + 1]
        if mk in (0xD8, 0x01) or 0xD0 <= mk <= 0xD7:
            i += 2
            continue
        if mk == 0xD9 or mk == 0xDA:   # the picture itself starts: nothing more to read
            break
        ln = struct.unpack(">H", data[i + 2:i + 4])[0]
        seg = data[i + 4:i + 2 + ln]
        if mk == 0xE1 and seg[:6] == b"Exif\0\0" and exif is None:
            exif = _tiff(seg[6:])
        elif 0xC0 <= mk <= 0xCF and mk not in (0xC4, 0xC8, 0xCC) and len(seg) >= 5:
            h, w = struct.unpack(">HH", seg[1:5])
            size = (w, h)
        i += 2 + ln
        if size and exif is not None:
            break
    return size, exif


def _webp(data):
    kind = data[12:16]
    if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", data[26:30])
        return (w & 0x3FFF, h & 0x3FFF)
    if kind == b"VP8L":
        b = data[21:25]
        return (1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6)))
    if kind == b"VP8X":
        return (1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"))
    return None


_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _tiff(t):
    """The tags of a TIFF block (EXIF is one): IFD0, with the Exif and GPS IFDs merged in (GPS under "gps")."""
    if t[:2] == b"II":
        e = "<"
    elif t[:2] == b"MM":
        e = ">"
    else:
        return {}

    def ifd(off, depth=0):
        tags = {}
        if off <= 0 or off + 2 > len(t) or depth > 3:
            return tags
        n = struct.unpack(e + "H", t[off:off + 2])[0]
        for k in range(min(n, 400)):
            p = off + 2 + k * 12
            if p + 12 > len(t):
                break
            tag, typ, cnt = struct.unpack(e + "HHI", t[p:p + 8])
            size = _SIZES.get(typ, 0) * cnt
            if not size:
                continue
            vp = p + 8 if size <= 4 else struct.unpack(e + "I", t[p + 8:p + 12])[0]
            raw = t[vp:vp + size]
            if len(raw) < size:
                continue
            if typ == 2:
                v = raw.split(b"\0")[0].decode("utf-8", "replace")
            elif typ in (5, 10):
                f = "I" if typ == 5 else "i"
                nums = struct.unpack(e + f * (2 * cnt), raw)
                vals = tuple(nums[j] / nums[j + 1] if nums[j + 1] else 0 for j in range(0, len(nums), 2))
                v = vals[0] if cnt == 1 else vals
            elif typ in (3, 4, 9):
                f = {3: "H", 4: "I", 9: "i"}[typ]
                vals = struct.unpack(e + f * cnt, raw)
                v = vals[0] if cnt == 1 else vals
            else:
                v = raw
            tags[tag] = v
        return tags

    root = ifd(struct.unpack(e + "I", t[4:8])[0])
    if isinstance(root.get(0x8769), int):
        root.update(ifd(root[0x8769], 1))
    if isinstance(root.get(0x8825), int):
        root["gps"] = ifd(root[0x8825], 1)
    return root


# ---- Video and sound ------------------------------------------------------------------------------------
def _clock(sec):
    sec = int(round(sec))
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def media_facts(path):
    exe = shutil.which("ffprobe")
    if not exe:
        return mdls_facts(path, media=True) if sys.platform == "darwin" else {"rows": []}
    r = subprocess.run([exe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
                       capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    info = json.loads(r.stdout or "{}")
    fmt = info.get("format") or {}
    rows = []
    dur = float(fmt.get("duration") or 0)
    if dur:
        rows.append(["Length", _clock(dur)])
    vid = next((s for s in info.get("streams") or [] if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    aud = next((s for s in info.get("streams") or [] if s.get("codec_type") == "audio"), None)
    if vid:
        w, h = vid.get("width"), vid.get("height")
        fps = vid.get("avg_frame_rate") or vid.get("r_frame_rate") or ""
        m = re.match(r"(\d+)/(\d+)", fps)
        rate = (int(m.group(1)) / int(m.group(2))) if m and int(m.group(2)) else 0
        rows.append(["Picture", ("%d × %d" % (w, h) if w and h else "") + (", %g fps" % round(rate, 2) if rate else "")])
        rows.append(["Video", (vid.get("codec_name") or "").upper()])
    if aud:
        ch = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(aud.get("channels"), "%s channels" % aud.get("channels") if aud.get("channels") else "")
        hz = int(aud.get("sample_rate") or 0)
        rows.append(["Sound", ", ".join(x for x in ((aud.get("codec_name") or "").upper(), ch, "%g kHz" % (hz / 1000) if hz else "") if x)])
    br = int(fmt.get("bit_rate") or 0)
    if br:
        rows.append(["Bit rate", "%.1f Mb/s" % (br / 1e6) if br >= 1e6 else "%d kb/s" % round(br / 1000)])
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for key, label in (("title", "Title"), ("artist", "Artist"), ("album_artist", "Album artist"), ("album", "Album"), ("date", "Year"), ("genre", "Genre"), ("track", "Track")):
        v = str(tags.get(key) or "").strip()
        if key == "album_artist" and v == str(tags.get("artist") or "").strip():
            continue
        rows.append([label, v[:4] if key == "date" and re.match(r"\d{4}-", v) else v])
    return {"rows": [r for r in rows if r[1]]}


# ---- PDF ----------------------------------------------------------------------------------------------------
def pdf_facts(path):
    exe = shutil.which("pdfinfo")
    if not exe:
        return mdls_facts(path) if sys.platform == "darwin" else {"rows": []}
    r = subprocess.run([exe, path], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    kv = {}
    for ln in r.stdout.splitlines():
        k, _, v = ln.partition(":")
        kv[k.strip()] = v.strip()
    rows = [["Pages", kv.get("Pages", "")], ["Title", kv.get("Title", "")], ["Author", kv.get("Author", "")], ["Subject", kv.get("Subject", "")]]
    size = kv.get("Page size", "")
    m = re.match(r"([\d.]+) x ([\d.]+) pts(?: \((\w+)\))?", size)
    if m:
        w, h = float(m.group(1)) / 72 * 25.4, float(m.group(2)) / 72 * 25.4
        rows.append(["Page size", (m.group(3) + ", " if m.group(3) else "") + "%d × %d mm" % (round(w), round(h))])
    made = kv.get("Creator") or kv.get("Producer") or ""
    rows.append(["Made with", made])
    if kv.get("Encrypted", "no").startswith("yes"):
        rows.append(["Protected", "Yes"])
    return {"rows": [r for r in rows if r[1]]}


# ---- Word, Excel, PowerPoint -------------------------------------------------------------------------------
def office_facts(path):
    rows = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())

        def tag(xml, name):
            m = re.search(r"<(?:\w+:)?" + name + r"(?:\s[^>]*)?>([^<]*)</", xml)
            return m.group(1).strip() if m else ""
        core = z.read("docProps/core.xml").decode("utf-8", "replace") if "docProps/core.xml" in names else ""
        app = z.read("docProps/app.xml").decode("utf-8", "replace") if "docProps/app.xml" in names else ""
        rows.append(["Title", tag(core, "title")])
        rows.append(["Author", tag(core, "creator")])
        last = tag(core, "lastModifiedBy")
        if last and last != tag(core, "creator"):
            rows.append(["Last saved by", last])
        for name, label in (("Pages", "Pages"), ("Words", "Words"), ("Slides", "Slides")):
            v = tag(app, name)
            if v and v != "0":
                rows.append([label, "{:,}".format(int(v)) if v.isdigit() else v])
        sheets = len([n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)])
        if sheets:
            rows.append(["Sheets", str(sheets)])
    from html import unescape
    return {"rows": [[k, unescape(v)] for k, v in rows if v]}


# ---- On a Mac, Spotlight knows -----------------------------------------------------------------------------
def mdls_facts(path, media=False):
    keys = ["kMDItemDurationSeconds", "kMDItemPixelWidth", "kMDItemPixelHeight", "kMDItemCodecs", "kMDItemTitle",
            "kMDItemAuthors", "kMDItemAlbum", "kMDItemNumberOfPages"]
    try:
        r = subprocess.run(["/usr/bin/mdls"] + sum((["-name", k] for k in keys), []) + [path], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return {"rows": []}
    kv = {}
    for m in re.finditer(r'^(\w+)\s+=\s+(\((?:.|\n)*?\)|.*)$', r.stdout, re.M):
        v = m.group(2).strip()
        if v == "(null)":
            continue
        if v.startswith("("):
            v = ", ".join(x.strip().strip('"') for x in v[1:-1].split(",") if x.strip())
        kv[m.group(1)] = v.strip('"')
    rows = []
    if kv.get("kMDItemDurationSeconds"):
        rows.append(["Length", _clock(float(kv["kMDItemDurationSeconds"]))])
    if kv.get("kMDItemPixelWidth") and kv.get("kMDItemPixelHeight"):
        rows.append(["Picture", "%s × %s" % (kv["kMDItemPixelWidth"], kv["kMDItemPixelHeight"])])
    rows += [["Codecs", kv.get("kMDItemCodecs", "")], ["Pages", kv.get("kMDItemNumberOfPages", "")], ["Title", kv.get("kMDItemTitle", "")],
             ["Artist" if media else "Author", kv.get("kMDItemAuthors", "")], ["Album", kv.get("kMDItemAlbum", "")]]
    return {"rows": [r for r in rows if r[1]]}
