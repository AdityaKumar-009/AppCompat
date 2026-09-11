#!/usr/bin/env python3
"""Harden special-app-access translation against OEM Settings differences."""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[special-access-oem] {label}: already applied")
            return text
        raise SystemExit(f"[special-access-oem] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[special-access-oem] {label}: expected one match, found {count}")
    print(f"[special-access-oem] {label}: applied")
    return text.replace(old, new, 1)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_special_access_oem_hardening.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacySpecialAccessCompat.java"
    text = path.read_text(encoding="utf-8")

    old = '''        if (NOTIFICATION_LISTENER_SETTINGS.equals(action)
                || NOTIFICATION_LISTENER_DETAIL_SETTINGS.equals(action)) {
            markNotificationListenerRequested(guestPackage);
            if (Build.VERSION.SDK_INT >= 30) {
                intent.setAction(NOTIFICATION_LISTENER_DETAIL_SETTINGS);
                intent.putExtra(
                        EXTRA_NOTIFICATION_LISTENER_COMPONENT,
                        new ComponentName(hostPackage, NOTIFICATION_LISTENER_PROXY));
                intent.setData(null);
            }
            return intent;
        }'''
    new = '''        if (NOTIFICATION_LISTENER_SETTINGS.equals(action)
                || NOTIFICATION_LISTENER_DETAIL_SETTINGS.equals(action)) {
            markNotificationListenerRequested(guestPackage);
            ComponentName proxy = new ComponentName(hostPackage, NOTIFICATION_LISTENER_PROXY);
            if (Build.VERSION.SDK_INT >= 30) {
                // AOSP exposes a component-specific page, but Android explicitly
                // permits a vendor Settings implementation not to provide it.
                // Probe before rewriting so a legacy app never receives an
                // ActivityNotFoundException solely because of OEM Settings.
                Intent detail = new Intent(NOTIFICATION_LISTENER_DETAIL_SETTINGS);
                detail.putExtra(EXTRA_NOTIFICATION_LISTENER_COMPONENT, proxy);
                try {
                    if (BlackBoxCore.getContext().getPackageManager()
                            .resolveActivity(detail, PackageManager.MATCH_DEFAULT_ONLY) != null) {
                        intent.setAction(NOTIFICATION_LISTENER_DETAIL_SETTINGS);
                        intent.putExtra(EXTRA_NOTIFICATION_LISTENER_COMPONENT, proxy);
                        intent.setData(null);
                        return intent;
                    }
                } catch (Throwable ignored) {
                }
            }
            // API 22-29 and OEMs without the detail Activity: the helper's declared
            // NotificationListenerService appears in the ordinary access list.
            intent.setAction(NOTIFICATION_LISTENER_SETTINGS);
            intent.removeExtra(EXTRA_NOTIFICATION_LISTENER_COMPONENT);
            intent.setData(null);
            return intent;
        }'''
    text = replace_once(text, old, new, "probe notification-listener detail Settings page")
    path.write_text(text, encoding="utf-8")

    if "resolveActivity(detail, PackageManager.MATCH_DEFAULT_ONLY)" not in text:
        raise SystemExit("[special-access-oem] verification failed")
    print("[special-access-oem] OEM Settings fallback verified")


if __name__ == "__main__":
    main()
