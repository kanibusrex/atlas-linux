#!/bin/bash
# Starts the built Atlas.app on a macOS CI machine and takes pictures of the screen, so the build can be
# looked at without a Mac: try/1-started.png, try/2-handed-over.png and try/atlas.log.
set -u
mkdir -p try
export ATLAS_LOG="$PWD/try/atlas.log" ATLAS_DEBUG=1
APP=dist/Atlas.app/Contents/MacOS/Atlas

fail() { echo "$1"; echo "---- atlas.log"; cat "$ATLAS_LOG"; exit 1; }

# the built app's own Python reaching GitHub (it has no certificates of its own; see atlas_update.py)
"$APP" --check-update 2>&1 | tee -a try/check-update.txt
[ "${PIPESTATUS[0]}" = 0 ] || fail "The built Atlas couldn't reach GitHub to look for updates."

"$APP" /System/Applications &   # only apps there, so their icons show
atlas=$!
sleep 25
screencapture -x try/1-started.png
kill -0 $atlas 2>/dev/null || fail "Atlas closed by itself."
grep -q "The page is up and talking to Atlas" "$ATLAS_LOG" || fail "Atlas's window opened, but its page never reached Atlas."

# a second Atlas hands its folder to the first (a new tab there), and leaves
start=$(date +%s)
"$APP" "$HOME" &
second=$!
for _ in $(seq 1 30); do kill -0 $second 2>/dev/null || break; sleep 1; done
kill -0 $second 2>/dev/null && fail "The second Atlas didn't hand over its folder and leave."
echo "The second Atlas left after $(( $(date +%s) - start )) s"
sleep 5
screencapture -x try/2-handed-over.png

osascript -e 'tell application "System Events" to set frontmost of (first process whose unix id is '"$atlas"') to true' \
          -e 'tell application "System Events" to keystroke "q" using command down' >/dev/null 2>&1
for _ in $(seq 1 15); do kill -0 $atlas 2>/dev/null || break; sleep 1; done
if kill -0 $atlas 2>/dev/null; then echo "Atlas didn't quit when asked (Cmd+Q); ended it."; kill $atlas; else echo "Atlas quit"; fi

# the preview on the right, with a document in it: all the PDF's pages, the Word file's formatting
mkdir -p try/samples
cp tests/samples/* try/samples/
python3 - "$HOME/Library/Application Support/Atlas/page.json" <<'PY'
import json, sys
try:
    page = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    page = {}
page.update(pvPos="right", preview=True)
json.dump(page, open(sys.argv[1], "w"))
PY
# somewhere macOS keeps private from apps, as it does Time Machine backups: the picture should say what to do
"$APP" "$HOME/Library/Safari" &
atlas=$!
sleep 15
screencapture -x try/4-kept-private.png
kill $atlas 2>/dev/null
sleep 3

for name in sample.pdf sample.docx; do
  before=$(grep -c "The page is up" "$ATLAS_LOG")
  "$APP" "$PWD/try/samples/$name" &
  atlas=$!
  sleep 20
  screencapture -x "try/3-$name.png"
  kill -0 $atlas 2>/dev/null || fail "Atlas closed by itself previewing $name."
  [ "$(grep -c "The page is up" "$ATLAS_LOG")" -gt "$before" ] || fail "Atlas never opened its page with $name."
  kill $atlas
  sleep 3
done
echo "---- atlas.log"
cat "$ATLAS_LOG"
