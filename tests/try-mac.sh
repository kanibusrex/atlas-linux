#!/bin/bash
# Starts the built Atlas.app on a macOS CI machine and takes pictures of the screen, so the build can be
# looked at without a Mac: try/1-started.png, try/2-handed-over.png and try/atlas.log.
set -u
mkdir -p try
export ATLAS_LOG="$PWD/try/atlas.log" ATLAS_DEBUG=1
APP=dist/Atlas.app/Contents/MacOS/Atlas

fail() { echo "$1"; echo "---- atlas.log"; cat "$ATLAS_LOG"; exit 1; }

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
echo "---- atlas.log"
cat "$ATLAS_LOG"
