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
# Vendor Settings can resolve the Android 11+ notification-listener detail action
# yet still crash on an OEM-specific implementation. Match Android's flattened-
# string extra contract, avoid the fragile detail surface on Xiaomi-family builds,
# and make grant detection tolerant of OEM framework/secure-setting timing.
python3 "$ROOT/scripts/patch_notification_settings_miui_hardening.py" "$DEST"
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
# Interactive legacy utilities (Floatify-style onboarding + overlays) expose a few
# edge cases only after user input: own-package PackageInfo nulls, OEM grant fallbacks
# whose action gets mutated, and legacy alert types that Android accepts with the
# wrong z-order. Apply these after the relevant compatibility helpers exist.
python3 "$ROOT/scripts/patch_legacy_interactive_hardening.py" "$DEST"
# Some onboarding screens immediately query version metadata and show a platform
# AlertDialog. Harden the server-side PackageInfo contract and ordinary application
# window token bridge after the general interactive/window patches are in place.
python3 "$ROOT/scripts/patch_legacy_dialog_hardening.py" "$DEST"
# After accepting an EULA, old utilities often check SYSTEM_ALERT_WINDOW and query
# running services. Keep the overlay grant truthful to the real helper UID/package,
# answer service discovery from the virtual registry, and expose a granted virtual
# NotificationListenerService as logically running immediately.
python3 "$ROOT/scripts/patch_legacy_service_gate_hardening.py" "$DEST"
# Real notification-listener permission must not eagerly instantiate large legacy
# background/overlay services while Settings is still returning to onboarding. Start
# virtual listeners lazily on their first real callback and preserve callback order.
python3 "$ROOT/scripts/patch_notification_listener_lifecycle.py" "$DEST"
# App-agnostic runtime restoration: real alarms, broadcast/service PendingIntents,
# manifest-receiver cold starts, resilient dynamic receiver registration and a real
# BOOT_COMPLETED wake/relay component. These contracts are shared by reminders,
# automation tools, widgets, sync clients and background utilities—not just Floatify.
python3 "$ROOT/scripts/patch_universal_legacy_runtime.py" "$DEST"
# Remove additional safe semantic no-ops: PendingIntent.send() now executes its real
# helper token and system-provider ContentObserver registration/notifyChange reaches
# Android instead of reporting success while doing nothing.
python3 "$ROOT/scripts/patch_universal_semantic_noops.py" "$DEST"
python3 "$ROOT/scripts/patch_universal_runtime_hardening.py" "$DEST"
# Upstream also fake-succeeds every PowerManager wakelock operation. Let the real
# helper UID own guest wakelocks so legacy alarms, players, downloads, navigation,
# BLE/sensor loggers and background services retain their expected CPU lifetime.
python3 "$ROOT/scripts/patch_universal_power_lifecycle.py" "$DEST"
# Modern Android validates AttributionSource UID/package pairs for real providers and
# Wi-Fi services. Normalize those calls to the helper identity, make failed read-only
# provider queries safely empty instead of null, and translate old hidden Wi-Fi AP
# calls to Local Only Hotspot where the platform exposes a legal third-party path.
python3 "$ROOT/scripts/patch_universal_provider_wifi_compat.py" "$DEST"
python3 "$ROOT/scripts/patch_sdk_gate_calls.py" "$DEST"

echo "Prepared patched compatibility engine $ENGINE_COMMIT"
