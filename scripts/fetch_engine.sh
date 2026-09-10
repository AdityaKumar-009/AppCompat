#!/usr/bin/env bash
set -euo pipefail

# Blacks-BlackBox currently carries newer Android 14-16 service, package,
# split-APK and OEM compatibility fixes than the older fork AppCompat used.
# Pin the exact revision so CI and released APKs remain reproducible.
ENGINE_REPO="https://github.com/Black00Z/Blacks-BlackBox.git"
ENGINE_COMMIT="40282a7bf4500948cfd598fc67e6e63114b26dd9"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/third_party/NewBlackbox"

if [ -d "$DEST/.git" ] && [ "$(git -C "$DEST" rev-parse HEAD 2>/dev/null || true)" = "$ENGINE_COMMIT" ]; then
  echo "Compatibility engine already pinned at $ENGINE_COMMIT"
  exit 0
fi

rm -rf "$DEST"
mkdir -p "$DEST"
git -C "$DEST" init -q
git -C "$DEST" remote add origin "$ENGINE_REPO"
git -C "$DEST" fetch --depth=1 origin "$ENGINE_COMMIT"
git -C "$DEST" checkout -q --detach FETCH_HEAD

echo "Fetched Blacks-BlackBox $ENGINE_COMMIT"
