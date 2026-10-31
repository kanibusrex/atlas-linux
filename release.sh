#!/bin/bash
# Releases the version in atlas.py:  ./release.sh notes.md
#   1. pushes main and waits for its build to pass
#   2. builds the AppImage
#   3. tags vX.Y.Z and makes the release as a DRAFT with the AppImage and the notes
#   4. the tag's build attaches the Windows installer and the Mac disk image, then publishes the release
#      (so Atlas never offers an update whose file for its system isn't there yet)
#   5. installs the new AppImage in ~/Applications
# A version is released once: bump VERSION in atlas.py for every release, never replace a release's files.
set -euo pipefail
cd "$(dirname "$0")"

notes=${1:-}
[ -f "$notes" ] || { echo "usage: $0 notes.md   (the release notes, Markdown)"; exit 1; }
v=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' atlas.py)
tag="v$v"
[ -z "$(git status --porcelain)" ] || { echo "Commit or put away your changes first."; git status --short; exit 1; }
[ "$(git branch --show-current)" = main ] || { echo "Release from main."; exit 1; }
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null || [ -n "$(git ls-remote --tags origin "$tag")" ] || gh release view "$tag" >/dev/null 2>&1; then
  echo "$tag already exists. Bump VERSION in atlas.py for a new release."; exit 1
fi

wait_for_build() {   # the Build run for this commit and ref, to its end
  local sha=$1 ref=$2 id=""
  for _ in $(seq 1 30); do
    id=$(gh run list --workflow build.yml --commit "$sha" --json databaseId,headBranch -q "map(select(.headBranch==\"$ref\"))[0].databaseId // empty")
    [ -n "$id" ] && break; sleep 5
  done
  [ -n "$id" ] || { echo "No build started for $ref."; return 1; }
  echo "Waiting for build $id ($ref)..."
  gh run watch "$id" --exit-status --interval 30 >/dev/null || { echo "Build $id failed: gh run view $id --log-failed"; return 1; }
}

sha=$(git rev-parse HEAD)
echo "== $tag from $(git log -1 --format='%h %s')"
git push -q origin main
wait_for_build "$sha" main

echo "== Building the AppImage"
./build-appimage.sh >/dev/null
img=dist/Atlas-x86_64.AppImage
[ -s "$img" ] || { echo "No AppImage was built."; exit 1; }

echo "== Tagging and making the draft release"
git tag "$tag" "$sha"
git push -q origin "$tag"
gh release create "$tag" "$img" --draft --title "Atlas $v" --notes-file "$notes" >/dev/null
wait_for_build "$sha" "$tag"
[ "$(gh release view "$tag" --json isDraft -q .isDraft)" = false ] || { echo "$tag is still a draft: see the build's publish job."; exit 1; }
echo "Published: $(gh release view "$tag" --json url -q .url)"
gh release view "$tag" --json assets -q '.assets[].name' | sed 's/^/  /'

echo "== Installing it here"
mkdir -p ~/Applications
cp "$img" ~/Applications/.Atlas-new.AppImage && mv ~/Applications/.Atlas-new.AppImage ~/Applications/Atlas-x86_64.AppImage   # mv: the running one may be busy
echo "Installed $v in ~/Applications"
