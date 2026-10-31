#!/usr/bin/env bash
# Install Atlas from an AppImage: puts it in ~/Applications, adds it to the app launcher, and
# (with --default) makes it the file manager folders open in, including "Show in folder" from other apps.
#
#   ./install.sh [path/to/Atlas-x86_64.AppImage] [--default]
set -euo pipefail

src="$(cd "$(dirname "$0")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
image="" default=0
for a in "$@"; do
  case "$a" in
    --default) default=1 ;;
    *) image="$a" ;;
  esac
done
[[ -z $image ]] && for c in "$src/Atlas-x86_64.AppImage" "$src/dist/Atlas-x86_64.AppImage" "$PWD/Atlas-x86_64.AppImage"; do
  [[ -f $c ]] && { image="$c"; break; }
done
[[ -f $image ]] || { echo "Usage: ./install.sh [path/to/Atlas-x86_64.AppImage] [--default]" >&2; exit 1; }

if ! python3 -c 'import gi; gi.require_version("WebKit2", "4.1")' 2>/dev/null; then
  echo "Atlas needs python-gobject and webkit2gtk-4.1:  sudo pacman -S python-gobject webkit2gtk-4.1" >&2
  exit 1
fi

mkdir -p "$HOME/Applications" "$data/applications" "$data/icons/hicolor/256x256/apps"
install -m 755 "$image" "$HOME/Applications/Atlas-x86_64.AppImage"
cp "$src/atlas.png" "$data/icons/hicolor/256x256/apps/atlas.png"
gtk-update-icon-cache "$data/icons/hicolor" &>/dev/null || true

sed "s|^Exec=.*|Exec=$HOME/Applications/Atlas-x86_64.AppImage %F|" "$src/atlas.desktop" >"$data/applications/atlas.desktop"
update-desktop-database "$data/applications" &>/dev/null || true
echo "Installed Atlas to ~/Applications/Atlas-x86_64.AppImage and the app launcher."

if (( default )); then
  xdg-mime default atlas.desktop inode/directory
  # "Show in folder" from browsers and other apps (org.freedesktop.FileManager1) starts Atlas
  mkdir -p "$data/dbus-1/services"
  cat >"$data/dbus-1/services/org.freedesktop.FileManager1.service" <<SERVICE
[D-BUS Service]
Name=org.freedesktop.FileManager1
Exec=$HOME/Applications/Atlas-x86_64.AppImage
SERVICE
  busctl --user call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus ReloadConfig &>/dev/null || true
  echo "Atlas is now the default file manager."
fi
