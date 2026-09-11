#!/usr/bin/env bash
set -euo pipefail

# Blacks-BlackBox carries the Android 14-16 service/package/split-APK hooks used by
# AppCompat. Pin the exact upstream revision for reproducible releases, then apply
# our audited compatibility fixes on top of that immutable source snapshot.
ENGINE_REPO="https://github.com/Black00Z/Blacks-BlackBox.git"
ENGINE_COMMIT="40282a7bf4500948cfd598fc67e6e63114b26dd9"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/third_party/NewBlackbox"

if [ -d "$DEST/.git" ] && [ "$(git -C "$DEST" rev-parse HEAD 2>/dev/null || true)" = "$ENGINE_COMMIT" ]; then
  echo "Compatibility engine already pinned at $ENGINE_COMMIT"
else
  rm -rf "$DEST"
  mkdir -p "$DEST"
  git -C "$DEST" init -q
  git -C "$DEST" remote add origin "$ENGINE_REPO"
  git -C "$DEST" fetch --depth=1 origin "$ENGINE_COMMIT"
  git -C "$DEST" checkout -q --detach FETCH_HEAD
  echo "Fetched Blacks-BlackBox $ENGINE_COMMIT"
fi

python3 "$ROOT/scripts/patch_engine.py" "$DEST"
python3 "$ROOT/scripts/patch_framework_translation.py" "$DEST"
python3 "$ROOT/scripts/patch_framework_translation_hardening.py" "$DEST"
python3 "$ROOT/scripts/patch_permission_translation.py" "$DEST"
python3 "$ROOT/scripts/patch_special_access_translation.py" "$DEST"
python3 "$ROOT/scripts/patch_special_access_oem_hardening.py" "$DEST"
python3 "$ROOT/scripts/patch_special_access_service_attribution.py" "$DEST"
python3 "$ROOT/scripts/patch_system_component_translation.py" "$DEST"
python3 "$ROOT/scripts/patch_guest_sdk_identity.py" "$DEST"
python3 "$ROOT/scripts/patch_guest_sdk_identity_hardening.py" "$DEST"
python3 "$ROOT/scripts/patch_guest_sdk_postparse.py" "$DEST"
# v13: raw equal-length string replacement is not DEX-safe because identifier tables
# are sorted. Replace that implementation with a structural dexlib2 rewrite and then
# run compile/deep-validation hardening before any Android compilation begins.
python3 "$ROOT/scripts/patch_guest_sdk_structural.py" "$DEST"
python3 "$ROOT/scripts/patch_guest_sdk_structural_hardening.py" "$DEST"
python3 "$ROOT/scripts/patch_legacy_window_translation.py" "$DEST"
python3 "$ROOT/scripts/patch_sdk_gate_calls.py" "$DEST"

echo "Prepared patched compatibility engine $ENGINE_COMMIT"
