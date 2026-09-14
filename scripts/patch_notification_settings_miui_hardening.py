#!/usr/bin/env python3
"""Harden notification-listener Settings and grant detection on OEM Android.

Observed failure mode on Xiaomi/MIUI:
* ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS resolves, so the generic OEM probe
  accepts it, but Settings itself crashes when launched with the wrong extra type.
* Android's documented/detail flow carries the listener component as a flattened
  string. Some AOSP paths are permissive, while vendor Settings code may cast the
  extra directly to String and crash if a ComponentName Parcelable is supplied.
* Some OEMs transiently report false from NotificationManager's direct grant query
  even though Settings.Secure.enabled_notification_listeners already contains the
  real helper component.

This patch therefore:
1. always serializes EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME as a flattened String;
2. bypasses the fragile component-detail surface on Xiaomi/Redmi/POCO and opens the
   ordinary notification-access list instead;
3. treats NotificationManager=true OR Settings.Secure containing the exact helper
   component as a real grant;
4. falls back to queryIntentServices() when legacy PackageInfo(GET_SERVICES) cannot
   enumerate the guest listener.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[notification-settings-oem] {label}: already applied")
            return text
        raise SystemExit(f"[notification-settings-oem] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[notification-settings-oem] {label}: expected one match, found {count}")
    print(f"[notification-settings-oem] {label}: applied")
    return text.replace(old, new, 1)


def patch_compat(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacySpecialAccessCompat.java"
    text = path.read_text(encoding="utf-8")

    if "import android.content.pm.ResolveInfo;" not in text:
        text = text.replace(
            "import android.content.pm.PackageManager;\nimport android.content.pm.ServiceInfo;",
            "import android.content.pm.PackageManager;\nimport android.content.pm.ResolveInfo;\nimport android.content.pm.ServiceInfo;",
            1,
        )

    old_block = '''            ComponentName proxy = new ComponentName(hostPackage, NOTIFICATION_LISTENER_PROXY);
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
            }'''
    new_block = '''            ComponentName proxy = new ComponentName(hostPackage, NOTIFICATION_LISTENER_PROXY);
            if (Build.VERSION.SDK_INT >= 30 && !preferSafeNotificationListenerList()) {
                // Android's own PermissionController passes this extra as the
                // flattened component *String*. Do the same: vendor Settings code
                // may cast directly to String and crash on a Parcelable ComponentName.
                Intent detail = new Intent(NOTIFICATION_LISTENER_DETAIL_SETTINGS);
                detail.putExtra(EXTRA_NOTIFICATION_LISTENER_COMPONENT, proxy.flattenToString());
                try {
                    if (BlackBoxCore.getContext().getPackageManager()
                            .resolveActivity(detail, PackageManager.MATCH_DEFAULT_ONLY) != null) {
                        intent.setAction(NOTIFICATION_LISTENER_DETAIL_SETTINGS);
                        intent.putExtra(
                                EXTRA_NOTIFICATION_LISTENER_COMPONENT,
                                proxy.flattenToString());
                        intent.setData(null);
                        return intent;
                    }
                } catch (Throwable ignored) {
                }
            }'''
    text = replace_once(text, old_block, new_block, "use flattened detail extra and MIUI-safe list")

    marker = '''    public static void markNotificationListenerRequested(String guestPackage) {'''
    helper = r'''    /**
     * Xiaomi-family Settings builds have shipped notification-listener detail
     * activities that resolve successfully but are less tolerant of third-party
     * deep-link inputs. The plain list action is stable and the dedicated helper is
     * visibly labelled with the guest app name, so prefer that surface there.
     */
    private static boolean preferSafeNotificationListenerList() {
        String manufacturer = Build.MANUFACTURER == null
                ? "" : Build.MANUFACTURER.toLowerCase(Locale.ROOT);
        String brand = Build.BRAND == null
                ? "" : Build.BRAND.toLowerCase(Locale.ROOT);
        return manufacturer.contains("xiaomi")
                || manufacturer.contains("redmi")
                || manufacturer.contains("poco")
                || brand.contains("xiaomi")
                || brand.contains("redmi")
                || brand.contains("poco");
    }

'''
    if "private static boolean preferSafeNotificationListenerList()" not in text:
        if marker not in text:
            raise SystemExit("[notification-settings-oem] helper insertion point not found")
        text = text.replace(marker, helper + marker, 1)
        print("[notification-settings-oem] Xiaomi/Redmi/POCO safe-list selector: applied")

    old_grant = '''            if (Build.VERSION.SDK_INT >= 27) {
                NotificationManager manager =
                        (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
                if (manager != null) {
                    return manager.isNotificationListenerAccessGranted(proxy);
                }
            }
            String enabled = Settings.Secure.getString('''
    new_grant = '''            if (Build.VERSION.SDK_INT >= 27) {
                NotificationManager manager =
                        (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
                if (manager != null) {
                    try {
                        if (manager.isNotificationListenerAccessGranted(proxy)) {
                            return true;
                        }
                    } catch (Throwable ignored) {
                        // Fall through to the secure-setting source used by legacy
                        // Android and by OEM builds during asynchronous Settings updates.
                    }
                }
            }
            String enabled = Settings.Secure.getString('''
    text = replace_once(text, old_grant, new_grant, "OR framework grant with secure enabled-listener state")

    old_components = '''    private static List<ComponentName> notificationListenerComponents(String guestPackage) {
        if (!isSafePackageName(guestPackage)) return Collections.emptyList();
        ArrayList<ComponentName> out = new ArrayList<>();
        try {
            PackageInfo info = BlackBoxCore.getBPackageManager().getPackageInfo(
                    guestPackage, PackageManager.GET_SERVICES, BlackBoxCore.getUserId());
            if (info == null || info.services == null) return out;
            for (ServiceInfo service : info.services) {
                if (service == null || service.name == null) continue;
                if (NOTIFICATION_LISTENER_PERMISSION.equals(service.permission)) {
                    out.add(new ComponentName(guestPackage, service.name));
                }
            }
        } catch (Throwable ignored) {
        }
        return out;
    }'''
    new_components = '''    private static List<ComponentName> notificationListenerComponents(String guestPackage) {
        if (!isSafePackageName(guestPackage)) return Collections.emptyList();
        ArrayList<ComponentName> out = new ArrayList<>();
        try {
            PackageInfo info = BlackBoxCore.getBPackageManager().getPackageInfo(
                    guestPackage, PackageManager.GET_SERVICES, BlackBoxCore.getUserId());
            if (info != null && info.services != null) {
                for (ServiceInfo service : info.services) {
                    if (service == null || service.name == null) continue;
                    if (NOTIFICATION_LISTENER_PERMISSION.equals(service.permission)) {
                        ComponentName component = new ComponentName(guestPackage, service.name);
                        if (!out.contains(component)) out.add(component);
                    }
                }
            }
        } catch (Throwable ignored) {
        }

        // Rich PackageInfo generation can fail for malformed/very old component
        // metadata. Intent resolution uses a different package-manager path and is
        // enough to discover NotificationListenerService implementations reliably.
        if (out.isEmpty()) {
            try {
                Intent query = new Intent("android.service.notification.NotificationListenerService");
                query.setPackage(guestPackage);
                List<ResolveInfo> resolved = BlackBoxCore.getBPackageManager().queryIntentServices(
                        query, PackageManager.GET_META_DATA, BlackBoxCore.getUserId());
                if (resolved != null) {
                    for (ResolveInfo resolve : resolved) {
                        ServiceInfo service = resolve == null ? null : resolve.serviceInfo;
                        if (service == null || service.name == null) continue;
                        if (NOTIFICATION_LISTENER_PERMISSION.equals(service.permission)) {
                            ComponentName component = new ComponentName(guestPackage, service.name);
                            if (!out.contains(component)) out.add(component);
                        }
                    }
                }
            } catch (Throwable ignored) {
            }
        }
        return out;
    }'''
    text = replace_once(text, old_components, new_components, "fallback listener discovery via queryIntentServices")

    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacySpecialAccessCompat.java"
    text = path.read_text(encoding="utf-8")
    required = (
        "proxy.flattenToString()",
        "preferSafeNotificationListenerList()",
        'manufacturer.contains("xiaomi")',
        "if (manager.isNotificationListenerAccessGranted(proxy))",
        "Settings.Secure.getString",
        "queryIntentServices(",
        "android.service.notification.NotificationListenerService",
        "import android.content.pm.ResolveInfo;",
    )
    for invariant in required:
        if invariant not in text:
            raise SystemExit(f"[notification-settings-oem] verification failed: {invariant}")
    if "detail.putExtra(EXTRA_NOTIFICATION_LISTENER_COMPONENT, proxy);" in text:
        raise SystemExit("[notification-settings-oem] raw ComponentName detail extra still present")
    print("[notification-settings-oem] notification Settings + grant detection verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_notification_settings_miui_hardening.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    patch_compat(root)
    verify(root)


if __name__ == "__main__":
    main()
