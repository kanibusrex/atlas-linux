#!/bin/sh
# Builds dist/Atlas-x86_64.AppImage (or into $OUT). Needs appimagetool on PATH, or APPIMAGETOOL=/path/to/it.
set -e
SRC="$(dirname "$(readlink -f "$0")")"
OUT="${OUT:-$SRC/dist}"; mkdir -p "$OUT"
APPDIR="$SRC/build/Atlas.AppDir"
rm -rf "$APPDIR"; mkdir -p "$APPDIR/usr/share/atlas"
cp "$SRC/atlas.py" "$SRC/atlas_core.py" "$SRC/atlas_portal.py" "$SRC/atlas.html" "$SRC/atlas.png" "$APPDIR/usr/share/atlas/"
cp "$SRC/AppRun" "$SRC/atlas.desktop" "$SRC/atlas.png" "$APPDIR/"
ln -s atlas.png "$APPDIR/.DirIcon"
ARCH="${ARCH:-x86_64}" "${APPIMAGETOOL:-appimagetool}" "$APPDIR" "$OUT/Atlas-x86_64.AppImage"
echo "Built $OUT/Atlas-x86_64.AppImage"
