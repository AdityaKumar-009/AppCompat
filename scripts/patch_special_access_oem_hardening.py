#!/usr/bin/env python3
"""Harden special-app-access translation against OEM Settings differences.

Legacy apps frequently open Settings with startActivityForResult(). A virtual
activity token cannot be used as a result target by the real Settings app. Route
recognized special-access intents as external no-result launches, exactly as modern
apps effectively use them (they re-check access from onResume()).
"""
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


def patch_compat(root: Path) -> None:
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

    marker = '''    public static void markNotificationListenerRequested(String guestPackage) {'''
    helpers = r'''    /** True only for Settings surfaces that AppCompat deliberately translates. */
    public static boolean isSpecialAccessSettingsIntent(Intent intent) {
        if (intent == null) return false;
        String action = intent.getAction();
        if (action == null) return false;
        return NOTIFICATION_LISTENER_SETTINGS.equals(action)
                || NOTIFICATION_LISTENER_DETAIL_SETTINGS.equals(action)
                || PACKAGE_SCOPED_SETTINGS_ACTIONS.contains(action)
                || "android.settings.USAGE_ACCESS_SETTINGS".equals(action)
                || "android.settings.NOTIFICATION_POLICY_ACCESS_SETTINGS".equals(action)
                || "android.settings.IGNORE_BATTERY_OPTIMIZATION_SETTINGS".equals(action)
                || "android.settings.APP_NOTIFICATION_SETTINGS".equals(action);
    }

    /**
     * Make a translated Settings intent resolvable on AOSP and OEM Android builds.
     * Some vendor Settings packages reject package: data for list-style pages or do
     * not implement the component-detail action. Degrade to the same page without
     * data, then app details, then the Settings root rather than crashing the guest.
     */
    public static void ensureResolvableSpecialAccessSettingsIntent(Intent intent) {
        if (!isSpecialAccessSettingsIntent(intent)) return;
        try {
            Context context = BlackBoxCore.getContext();
            PackageManager pm = context.getPackageManager();
            if (pm.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY) != null) return;

            String action = intent.getAction();
            // Many OEM list-style Settings activities reject package: deep links.
            intent.setComponent(null);
            intent.setPackage(null);
            intent.setData(null);
            if (pm.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY) != null) return;

            if (NOTIFICATION_LISTENER_DETAIL_SETTINGS.equals(action)) {
                intent.setAction(NOTIFICATION_LISTENER_SETTINGS);
                intent.removeExtra(EXTRA_NOTIFICATION_LISTENER_COMPONENT);
                if (pm.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY) != null) return;
            }

            intent.setAction(Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
            intent.setData(Uri.fromParts("package", BlackBoxCore.getHostPkg(), null));
            if (pm.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY) != null) return;

            // Last-resort Settings root is preferable to killing a legacy onboarding
            // Activity with ActivityNotFoundException.
            intent.setAction(Settings.ACTION_SETTINGS);
            intent.setData(null);
            intent.setComponent(null);
            intent.setPackage(null);
        } catch (Throwable ignored) {
            // The caller still has the translated intent; ActivityManager's external
            // routing guard below prevents an invalid virtual result target.
        }
    }

'''
    if "isSpecialAccessSettingsIntent(Intent intent)" not in text:
        if marker not in text:
            raise SystemExit("[special-access-oem] compat helper insertion point not found")
        text = text.replace(marker, helpers + marker, 1)
        print("[special-access-oem] resolvable special-access intent helpers: applied")

    path.write_text(text, encoding="utf-8")


def patch_activity_manager(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/ActivityManagerCommonProxy.java"
    text = path.read_text(encoding="utf-8")

    old_rewrite = '''            LegacySpecialAccessCompat.rewriteSettingsIntent(
                    intent, BActivityThread.getAppPackageName());'''
    new_rewrite = '''            LegacySpecialAccessCompat.rewriteSettingsIntent(
                    intent, BActivityThread.getAppPackageName());
            LegacySpecialAccessCompat.ensureResolvableSpecialAccessSettingsIntent(intent);'''
    text = replace_once(text, old_rewrite, new_rewrite, "normalize translated Settings intent")

    old_open = '''            if (!AppSystemEnv.isOpenPackage(hostResolve.activityInfo.packageName)) {
                return;
            }

            boolean didRedirectSamsungAccount = false;'''
    new_open = '''            // Special-access Settings is a deliberately translated system surface.
            // com.android.settings (and OEM equivalents) is not normally in the
            // BlackBox open-package allowlist, so authorize only these recognized
            // Settings actions without broadening external package access.
            boolean specialAccessSettings =
                    LegacySpecialAccessCompat.isSpecialAccessSettingsIntent(intent);
            if (!specialAccessSettings
                    && !AppSystemEnv.isOpenPackage(hostResolve.activityInfo.packageName)) {
                return;
            }

            boolean didRedirectSamsungAccount = false;'''
    text = replace_once(text, old_open, new_open, "allow translated Settings through external guard")

    old_result = '''            int requestCode = StartActivityCompat.getRequestCode(args);
            if (requestCode >= 0 && !didRedirectSamsungAccount) {
                return;
            }'''
    new_result = '''            int requestCode = StartActivityCompat.getRequestCode(args);
            if (requestCode >= 0 && !didRedirectSamsungAccount && !specialAccessSettings) {
                return;
            }
            if (requestCode >= 0 && specialAccessSettings) {
                // Legacy apps commonly call startActivityForResult() for Settings.
                // The real Settings task cannot deliver a result to a virtual guest
                // activity token. Strip that linkage and let onResume() perform the
                // standard permission re-check instead of crashing/restarting.
                Slog.d(TAG, "Converting special-access startActivityForResult to safe external launch");
            }'''
    text = replace_once(text, old_result, new_result, "strip invalid virtual result target for Settings")

    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    compat = (root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacySpecialAccessCompat.java").read_text(encoding="utf-8")
    activity = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/ActivityManagerCommonProxy.java").read_text(encoding="utf-8")
    required = [
        "resolveActivity(detail, PackageManager.MATCH_DEFAULT_ONLY)",
        "isSpecialAccessSettingsIntent(Intent intent)",
        "ensureResolvableSpecialAccessSettingsIntent(Intent intent)",
    ]
    if any(item not in compat for item in required):
        raise SystemExit("[special-access-oem] compat verification failed")
    if "!didRedirectSamsungAccount && !specialAccessSettings" not in activity:
        raise SystemExit("[special-access-oem] startActivityForResult guard verification failed")
    if "LegacySpecialAccessCompat.ensureResolvableSpecialAccessSettingsIntent(intent);" not in activity:
        raise SystemExit("[special-access-oem] Settings normalization verification failed")
    print("[special-access-oem] OEM + startActivityForResult routing verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_special_access_oem_hardening.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    patch_compat(root)
    patch_activity_manager(root)
    verify(root)


if __name__ == "__main__":
    main()
