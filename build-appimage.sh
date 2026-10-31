#!/bin/sh
# Builds dist/Atlas-x86_64.AppImage (or into $OUT). Needs appimagetool on PATH, or APPIMAGETOOL=/path/to/it.
set -e
SRC="$(dirname "$(readlink -f "$0")")"
OUT="${OUT:-$SRC/dist}"; mkdir -p "$OUT"
# pywebview and what it needs (all plain Python), fetched once into lib/ with a pip of Atlas's own
if [ ! -d "$SRC/lib/webview" ]; then
  rm -rf "$SRC/build/venv" "$SRC/lib"
  python3 -m venv "$SRC/build/venv"
  "$SRC/build/venv/bin/python" -m pip install -q --no-compile --target "$SRC/lib" -r "$SRC/requirements.txt"
  rm -rf "$SRC/lib/bin" "$SRC/build/venv"
fi
APPDIR="$SRC/build/Atlas.AppDir"
rm -rf "$APPDIR"; mkdir -p "$APPDIR/usr/share/atlas"
cp "$SRC/atlas.py" "$SRC/atlas_core.py" "$SRC/atlas_linux.py" "$SRC/atlas_portal.py" "$SRC/atlas.html" "$SRC/atlas.png" "$APPDIR/usr/share/atlas/"
cp -r "$SRC/lib" "$APPDIR/usr/share/atlas/lib"
find "$APPDIR/usr/share/atlas/lib" -name __pycache__ -prune -exec rm -rf {} +
cp "$SRC/AppRun" "$SRC/atlas.desktop" "$SRC/atlas.png" "$APPDIR/"
ln -s atlas.png "$APPDIR/.DirIcon"
ARCH="${ARCH:-x86_64}" "${APPIMAGETOOL:-appimagetool}" "$APPDIR" "$OUT/Atlas-x86_64.AppImage"
echo "Built $OUT/Atlas-x86_64.AppImage"
