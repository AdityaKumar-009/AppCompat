#!/usr/bin/env python3
"""Harden interactive legacy-app paths exposed by Floatify-style utilities.

This patch intentionally stays generic. It fixes three runtime edge cases that old
Android utilities commonly exercise after their first screen:

1. PackageManager.getPackageInfo() for the *current installed guest* must not return
   null merely because a non-zero flag set could not be materialized. Old apps often
   dereference versionCode/versionName immediately (for example, EULA/changelog code).
   Retry the same virtual package with flags=0 before falling through.
2. Settings grant intents may be degraded to a generic OEM Settings/Security page.
   Preserve an internal marker so the ActivityManager bridge still recognizes that
   mutated intent as an explicitly translated external grant flow, including when the
   guest used startActivityForResult().
3. On Android O+, legacy SYSTEM_ALERT_WINDOW types can be accepted but deliberately
   assigned downgraded layering. Translate the known pre-Oreo alert types to
   TYPE_APPLICATION_OVERLAY *before* the real WindowManager call instead of waiting
   for a hard rejection. This cannot bypass modern critical-system-window security;
   it only requests the modern representation Android defines for these old types.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[legacy-interactive] {label}: already applied")
            return text
        raise SystemExit(f"[legacy-interactive] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(
            f"[legacy-interactive] {label}: expected one match, found {count}"
        )
    print(f"[legacy-interactive] {label}: applied")
    return text.replace(old, new, 1)


def patch_current_guest_package_info(root: Path) -> None:
    path = root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/"
        "IPackageManagerProxy.java"
    )
    text = path.read_text(encoding="utf-8")
    old = '''            PackageInfo packageInfo = BlackBoxCore.getBPackageManager().getPackageInfo(packageName, flags, BlackBoxCore.getUserId());
            if (packageInfo != null) {'''
    new = '''            PackageInfo packageInfo = BlackBoxCore.getBPackageManager().getPackageInfo(
                    packageName, flags, BlackBoxCore.getUserId());
            if (packageInfo == null && flags != 0 && packageName != null
                    && packageName.equals(BActivityThread.getAppPackageName())
                    && BlackBoxCore.get().isInstalled(packageName, BlackBoxCore.getUserId())) {
                // PackageManager#getPackageInfo for an installed package is expected
                // to return PackageInfo (or throw NameNotFoundException), never a
                // synthetic null. Some old onboarding/EULA helpers request
                // GET_ACTIVITIES only to read versionCode/versionName and then
                // dereference the result immediately. If the richer virtual object
                // cannot be materialized, preserve that contract with the package's
                // base metadata rather than crashing the guest.
                packageInfo = BlackBoxCore.getBPackageManager().getPackageInfo(
                        packageName, 0, BlackBoxCore.getUserId());
                if (packageInfo != null) {
                    Slog.w(TAG, "Retried current guest PackageInfo without flags: " + packageName);
                }
            }
            if (packageInfo != null) {'''
    text = replace_once(
        text,
        old,
        new,
        "retry current guest PackageInfo without optional flags",
    )
    path.write_text(text, encoding="utf-8")


def patch_special_access_route_marker(root: Path) -> None:
    path = root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/"
        "LegacySpecialAccessCompat.java"
    )
    text = path.read_text(encoding="utf-8")

    old_constant = '''    private static final String ENABLED_NOTIFICATION_LISTENERS =
            "enabled_notification_listeners";'''
    new_constant = '''    private static final String ENABLED_NOTIFICATION_LISTENERS =
            "enabled_notification_listeners";
    private static final String EXTRA_TRANSLATED_SPECIAL_ACCESS =
            "com.appcompat.engine.extra.TRANSLATED_SPECIAL_ACCESS";'''
    text = replace_once(
        text,
        old_constant,
        new_constant,
        "add special-access route marker",
    )

    old_classifier = '''    public static boolean isSpecialAccessSettingsIntent(Intent intent) {
        if (intent == null) return false;
        String action = intent.getAction();'''
    new_classifier = '''    public static boolean isSpecialAccessSettingsIntent(Intent intent) {
        if (intent == null) return false;
        if (intent.getBooleanExtra(EXTRA_TRANSLATED_SPECIAL_ACCESS, false)) return true;
        String action = intent.getAction();'''
    text = replace_once(
        text,
        old_classifier,
        new_classifier,
        "recognize mutated special-access fallback intents",
    )

    old_ensure = '''    public static void ensureResolvableSpecialAccessSettingsIntent(Intent intent) {
        if (!isSpecialAccessSettingsIntent(intent)) return;
        try {'''
    new_ensure = '''    public static void ensureResolvableSpecialAccessSettingsIntent(Intent intent) {
        if (!isSpecialAccessSettingsIntent(intent)) return;
        // Keep the classification after OEM fallback mutates the action to a
        // generic Settings surface. ActivityManager later uses this marker to
        // authorize the deliberate external launch and strip a virtual result token.
        intent.putExtra(EXTRA_TRANSLATED_SPECIAL_ACCESS, true);
        try {'''
    text = replace_once(
        text,
        old_ensure,
        new_ensure,
        "retain special-access identity across OEM fallback",
    )
    path.write_text(text, encoding="utf-8")


def patch_system_grant_route_marker(root: Path) -> None:
    path = root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/"
        "LegacySystemComponentCompat.java"
    )
    text = path.read_text(encoding="utf-8")

    old_constant = '''    private static final String DEVICE_ADMIN_PROXY =
            "com.appcompat.engine.LegacyDeviceAdminReceiver";'''
    new_constant = '''    private static final String DEVICE_ADMIN_PROXY =
            "com.appcompat.engine.LegacyDeviceAdminReceiver";
    private static final String EXTRA_TRANSLATED_SYSTEM_GRANT =
            "com.appcompat.engine.extra.TRANSLATED_SYSTEM_GRANT";'''
    text = replace_once(
        text,
        old_constant,
        new_constant,
        "add system-component grant route marker",
    )

    old_classifier = '''    public static boolean isSystemGrantIntent(Intent intent) {
        if (intent == null) return false;
        String action = intent.getAction();'''
    new_classifier = '''    public static boolean isSystemGrantIntent(Intent intent) {
        if (intent == null) return false;
        if (intent.getBooleanExtra(EXTRA_TRANSLATED_SYSTEM_GRANT, false)) return true;
        String action = intent.getAction();'''
    text = replace_once(
        text,
        old_classifier,
        new_classifier,
        "recognize mutated component-grant fallback intents",
    )

    old_ensure = '''    public static void ensureResolvableSystemGrantIntent(Intent intent) {
        if (!isSystemGrantIntent(intent)) return;
        try {'''
    new_ensure = '''    public static void ensureResolvableSystemGrantIntent(Intent intent) {
        if (!isSystemGrantIntent(intent)) return;
        intent.putExtra(EXTRA_TRANSLATED_SYSTEM_GRANT, true);
        try {'''
    text = replace_once(
        text,
        old_ensure,
        new_ensure,
        "retain component-grant identity across OEM fallback",
    )
    path.write_text(text, encoding="utf-8")


def patch_eager_legacy_overlay_translation(root: Path) -> None:
    path = root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/"
        "IWindowSessionProxy.java"
    )
    text = path.read_text(encoding="utf-8")

    old = '''            final int originalType = params.type;
            try {
                Object first = method.invoke(who, args);
                if (!isRejectedAddResult(first)) {
                    return first;
                }
                params.type = WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY;
                Slog.w(TAG, "Legacy alert type " + originalType
                        + " was rejected by system_server; retrying as TYPE_APPLICATION_OVERLAY");
                return method.invoke(who, args);
            } catch (Throwable firstFailure) {
                Throwable cause = unwrapInvocation(firstFailure);
                if (!isWindowPermissionFailure(cause)) {
                    throw firstFailure;
                }
                params.type = WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY;
                Slog.w(TAG, "Legacy alert type " + originalType
                        + " failed with " + cause.getClass().getSimpleName()
                        + "; retrying as TYPE_APPLICATION_OVERLAY");
                return method.invoke(who, args);
            }'''
    new = '''            final int originalType = params.type;
            // Android O+ may *accept* these pre-O alert types while assigning them
            // deliberately downgraded layering. A success result therefore does not
            // mean the old semantic survived. Translate before crossing into
            // system_server so legacy overlays receive Android's modern SAW type.
            params.type = WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY;
            Slog.d(TAG, "Translating legacy alert type " + originalType
                    + " to TYPE_APPLICATION_OVERLAY before addToDisplay");
            return method.invoke(who, args);'''
    text = replace_once(
        text,
        old,
        new,
        "translate legacy alert windows before system_server",
    )
    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    pm = (root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/"
        "IPackageManagerProxy.java"
    )).read_text(encoding="utf-8")
    special = (root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/"
        "LegacySpecialAccessCompat.java"
    )).read_text(encoding="utf-8")
    system = (root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/"
        "LegacySystemComponentCompat.java"
    )).read_text(encoding="utf-8")
    window = (root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/"
        "IWindowSessionProxy.java"
    )).read_text(encoding="utf-8")

    required = [
        (pm, "Retried current guest PackageInfo without flags"),
        (special, "EXTRA_TRANSLATED_SPECIAL_ACCESS"),
        (special, "intent.putExtra(EXTRA_TRANSLATED_SPECIAL_ACCESS, true)"),
        (system, "EXTRA_TRANSLATED_SYSTEM_GRANT"),
        (system, "intent.putExtra(EXTRA_TRANSLATED_SYSTEM_GRANT, true)"),
        (window, "Translating legacy alert type"),
        (window, "params.type = WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY"),
    ]
    for text, invariant in required:
        if invariant not in text:
            raise SystemExit(f"[legacy-interactive] verification failed: {invariant}")

    if "was rejected by system_server; retrying as TYPE_APPLICATION_OVERLAY" in window:
        raise SystemExit("[legacy-interactive] late-only overlay translation still active")

    print("[legacy-interactive] interactive legacy hardening verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_legacy_interactive_hardening.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    patch_current_guest_package_info(root)
    patch_special_access_route_marker(root)
    patch_system_grant_route_marker(root)
    patch_eager_legacy_overlay_translation(root)
    verify(root)


if __name__ == "__main__":
    main()
