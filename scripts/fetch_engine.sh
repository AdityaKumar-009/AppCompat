#!/usr/bin/env bash
set -euo pipefail

ENGINE_REPO="https://github.com/ALEX5402/NewBlackbox.git"
ENGINE_COMMIT="89b59836c66f173756a4ae258cf379a957649820"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/third_party/NewBlackbox"

if [ -d "$DEST/.git" ] && [ "$(git -C "$DEST" rev-parse HEAD 2>/dev/null || true)" = "$ENGINE_COMMIT" ]; then
  echo "NewBlackbox already pinned at $ENGINE_COMMIT"
  exit 0
fi

rm -rf "$DEST"
mkdir -p "$DEST"
git -C "$DEST" init -q
git -C "$DEST" remote add origin "$ENGINE_REPO"
git -C "$DEST" fetch --depth=1 origin "$ENGINE_COMMIT"
git -C "$DEST" checkout -q --detach FETCH_HEAD

echo "Fetched NewBlackbox $ENGINE_COMMIT"
