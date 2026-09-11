#!/usr/bin/env python3
"""Patch the pinned engine with a truthful guest->host runtime-permission bridge.

Virtual guest packages do not exist in Android's real PackageManager, so forwarding
legacy permission checks/requests with the guest package name produces denials or
SecurityException/NameNotFound failures. The helper package is the real Linux/Android
permission principal. Translate the guest's declared permissions to permissions the
helper must hold on the running Android release, and make guest checks mirror those
real grants.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[permission-translation] {label}: already applied")
            return text
        raise SystemExit(f"[permission-translation] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[permission-translation] {label}: expected one match, found {count}")
    print(f"[permission-translation] {label}: applied")
    return text.replace(old, new, 1)


def write_permission_compat(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyPermissionCompat.java"
    source = r'''package top.niunaijun.blackbox.utils.compat;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.PermissionInfo;
import android.os.Build;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

import top.niunaijun.blackbox.BlackBoxCore;

/**
 * Maps a virtual guest's permission model onto the helper package that actually
 * owns the Android UID. This deliberately does not auto-grant sensitive access:
 * dangerous permissions are considered granted only when Android has granted the
 * corresponding permission to the helper runtime. Legacy storage writes remain
 * sandboxed by BlackBox and therefore do not require broad modern storage access.
 */
public final class LegacyPermissionCompat {
    private LegacyPermissionCompat() {
    }

    public static String[] requiredHostRuntimePermissions(String guestPackage, int userId) {
        Context context = BlackBoxCore.getContext();
        if (context == null || guestPackage == null) {
            return new String[0];
        }

        PackageInfo guest = null;
        try {
            guest = BlackBoxCore.getBPackageManager().getPackageInfo(
                    guestPackage, PackageManager.GET_PERMISSIONS, userId);
        } catch (Throwable ignored) {
        }
        if (guest == null || guest.requestedPermissions == null) {
            return new String[0];
        }

        Set<String> hostDeclared = hostDeclaredPermissions(context);
        LinkedHashSet<String> out = new LinkedHashSet<>();
        for (String guestPermission : guest.requestedPermissions) {
            for (String mapped : mapToHostPermissions(guestPermission)) {
                if (mapped == null || !hostDeclared.contains(mapped)) {
                    continue;
                }
                if (!isDangerousPermission(context, mapped)) {
                    continue;
                }
                // ACCESS_BACKGROUND_LOCATION must be requested separately after a
                // foreground location grant. Do not put it in the initial batch.
                if (Manifest.permission.ACCESS_BACKGROUND_LOCATION.equals(mapped)) {
                    continue;
                }
                if (context.checkSelfPermission(mapped) != PackageManager.PERMISSION_GRANTED) {
                    out.add(mapped);
                }
            }
        }
        return out.toArray(new String[0]);
    }

    /**
     * Returns null when this bridge does not own the decision. Otherwise returns
     * PackageManager.PERMISSION_GRANTED/DENIED for the virtual guest.
     */
    public static Integer checkGuestPermission(String permission, String guestPackage) {
        Context context = BlackBoxCore.getContext();
        if (context == null || permission == null || guestPackage == null) {
            return null;
        }
        if (!guestRequestedPermission(guestPackage, permission)) {
            return null;
        }

        // Legacy external storage is redirected into the virtual filesystem. It is
        // therefore safe to preserve the old logical grant without claiming broad
        // filesystem access to modern Android shared storage.
        if (Manifest.permission.WRITE_EXTERNAL_STORAGE.equals(permission)
                && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            return PackageManager.PERMISSION_GRANTED;
        }
        if (Manifest.permission.READ_EXTERNAL_STORAGE.equals(permission)
                && Build.VERSION.SDK_INT >= 33) {
            return PackageManager.PERMISSION_GRANTED;
        }

        Integer protection = permissionProtectionBase(context, permission);
        if (protection != null && protection == PermissionInfo.PROTECTION_NORMAL) {
            return PackageManager.PERMISSION_GRANTED;
        }

        List<String> mapped = mapToHostPermissions(permission);
        if (mapped.isEmpty()) {
            return null;
        }

        // A few old umbrella permissions fan out into several modern permissions.
        // For a logical guest check, any granted modern media permission is enough
        // to let the app continue; Android still enforces actual per-resource access.
        boolean anySemantics = Manifest.permission.READ_EXTERNAL_STORAGE.equals(permission)
                || Manifest.permission.BLUETOOTH.equals(permission)
                || Manifest.permission.BLUETOOTH_ADMIN.equals(permission);

        boolean sawRuntimePermission = false;
        boolean anyGranted = false;
        boolean allGranted = true;
        Set<String> hostDeclared = hostDeclaredPermissions(context);
        for (String hostPermission : mapped) {
            if (!hostDeclared.contains(hostPermission)) {
                continue;
            }
            Integer hostProtection = permissionProtectionBase(context, hostPermission);
            if (hostProtection != null && hostProtection == PermissionInfo.PROTECTION_NORMAL) {
                anyGranted = true;
                continue;
            }
            if (hostProtection == null || hostProtection != PermissionInfo.PROTECTION_DANGEROUS) {
                continue;
            }
            sawRuntimePermission = true;
            boolean granted = context.checkSelfPermission(hostPermission)
                    == PackageManager.PERMISSION_GRANTED;
            anyGranted |= granted;
            allGranted &= granted;
        }

        if (!sawRuntimePermission) {
            // Signature/privileged permissions must stay with Android's real
            // enforcement path. Never fabricate these as granted.
            return null;
        }
        return (anySemantics ? anyGranted : allGranted)
                ? PackageManager.PERMISSION_GRANTED
                : PackageManager.PERMISSION_DENIED;
    }

    public static boolean handlesGuestPermission(String permission, String guestPackage) {
        return checkGuestPermission(permission, guestPackage) != null;
    }

    private static boolean guestRequestedPermission(String packageName, String permission) {
        try {
            PackageInfo info = BlackBoxCore.getBPackageManager().getPackageInfo(
                    packageName, PackageManager.GET_PERMISSIONS, BlackBoxCore.getUserId());
            return info != null && info.requestedPermissions != null
                    && Arrays.asList(info.requestedPermissions).contains(permission);
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static List<String> mapToHostPermissions(String permission) {
        if (permission == null) {
            return Collections.emptyList();
        }

        if (Manifest.permission.ACCESS_FINE_LOCATION.equals(permission)) {
            return Arrays.asList(
                    Manifest.permission.ACCESS_COARSE_LOCATION,
                    Manifest.permission.ACCESS_FINE_LOCATION);
        }
        if (Manifest.permission.ACCESS_COARSE_LOCATION.equals(permission)) {
            return Collections.singletonList(Manifest.permission.ACCESS_COARSE_LOCATION);
        }
        if (Manifest.permission.ACCESS_BACKGROUND_LOCATION.equals(permission)) {
            if (Build.VERSION.SDK_INT >= 29) {
                return Arrays.asList(
                        Manifest.permission.ACCESS_COARSE_LOCATION,
                        Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.ACCESS_BACKGROUND_LOCATION);
            }
            return Collections.singletonList(Manifest.permission.ACCESS_FINE_LOCATION);
        }

        if (Manifest.permission.READ_EXTERNAL_STORAGE.equals(permission)) {
            if (Build.VERSION.SDK_INT >= 33) {
                return Arrays.asList(
                        "android.permission.READ_MEDIA_IMAGES",
                        "android.permission.READ_MEDIA_VIDEO",
                        "android.permission.READ_MEDIA_AUDIO");
            }
            return Collections.singletonList(Manifest.permission.READ_EXTERNAL_STORAGE);
        }
        if (Manifest.permission.WRITE_EXTERNAL_STORAGE.equals(permission)) {
            return Build.VERSION.SDK_INT >= 29
                    ? Collections.<String>emptyList()
                    : Collections.singletonList(Manifest.permission.WRITE_EXTERNAL_STORAGE);
        }

        if (Manifest.permission.BLUETOOTH.equals(permission)) {
            if (Build.VERSION.SDK_INT >= 31) {
                return Collections.singletonList("android.permission.BLUETOOTH_CONNECT");
            }
            return Collections.singletonList(Manifest.permission.BLUETOOTH);
        }
        if (Manifest.permission.BLUETOOTH_ADMIN.equals(permission)) {
            if (Build.VERSION.SDK_INT >= 31) {
                return Arrays.asList(
                        "android.permission.BLUETOOTH_SCAN",
                        "android.permission.BLUETOOTH_CONNECT",
                        "android.permission.BLUETOOTH_ADVERTISE");
            }
            return Collections.singletonList(Manifest.permission.BLUETOOTH_ADMIN);
        }

        // Modern split permissions can pass through directly when the running
        // platform knows them. Strings avoid verifier issues on old Android APIs.
        if (permission.startsWith("android.permission.READ_MEDIA_")
                || permission.equals("android.permission.POST_NOTIFICATIONS")
                || permission.equals("android.permission.NEARBY_WIFI_DEVICES")
                || permission.equals("android.permission.BLUETOOTH_SCAN")
                || permission.equals("android.permission.BLUETOOTH_CONNECT")
                || permission.equals("android.permission.BLUETOOTH_ADVERTISE")) {
            return Collections.singletonList(permission);
        }

        // Dangerous permissions whose names remained stable across releases.
        if (Manifest.permission.CAMERA.equals(permission)
                || Manifest.permission.RECORD_AUDIO.equals(permission)
                || Manifest.permission.READ_CONTACTS.equals(permission)
                || Manifest.permission.WRITE_CONTACTS.equals(permission)
                || Manifest.permission.GET_ACCOUNTS.equals(permission)
                || Manifest.permission.READ_CALENDAR.equals(permission)
                || Manifest.permission.WRITE_CALENDAR.equals(permission)
                || Manifest.permission.READ_PHONE_STATE.equals(permission)
                || Manifest.permission.READ_PHONE_NUMBERS.equals(permission)
                || Manifest.permission.CALL_PHONE.equals(permission)
                || Manifest.permission.READ_CALL_LOG.equals(permission)
                || Manifest.permission.WRITE_CALL_LOG.equals(permission)
                || Manifest.permission.SEND_SMS.equals(permission)
                || Manifest.permission.RECEIVE_SMS.equals(permission)
                || Manifest.permission.READ_SMS.equals(permission)
                || Manifest.permission.RECEIVE_MMS.equals(permission)
                || Manifest.permission.BODY_SENSORS.equals(permission)
                || Manifest.permission.ACTIVITY_RECOGNITION.equals(permission)
                || Manifest.permission.ACCESS_MEDIA_LOCATION.equals(permission)) {
            return Collections.singletonList(permission);
        }

        return Collections.singletonList(permission);
    }

    private static Set<String> hostDeclaredPermissions(Context context) {
        try {
            PackageInfo info = context.getPackageManager().getPackageInfo(
                    context.getPackageName(), PackageManager.GET_PERMISSIONS);
            if (info.requestedPermissions == null) {
                return Collections.emptySet();
            }
            return new LinkedHashSet<>(Arrays.asList(info.requestedPermissions));
        } catch (Throwable ignored) {
            return Collections.emptySet();
        }
    }

    private static boolean isDangerousPermission(Context context, String permission) {
        Integer base = permissionProtectionBase(context, permission);
        return base != null && base == PermissionInfo.PROTECTION_DANGEROUS;
    }

    @SuppressWarnings("deprecation")
    private static Integer permissionProtectionBase(Context context, String permission) {
        try {
            PermissionInfo info = context.getPackageManager().getPermissionInfo(permission, 0);
            return info.protectionLevel & PermissionInfo.PROTECTION_MASK_BASE;
        } catch (Throwable ignored) {
            return null;
        }
    }
}
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    print("[permission-translation] LegacyPermissionCompat: written")


def patch_package_manager(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IPackageManagerProxy.java"
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "import top.niunaijun.blackbox.utils.compat.BuildCompat;\n"
        "import top.niunaijun.blackbox.utils.compat.ParceledListSliceCompat;",
        "import top.niunaijun.blackbox.utils.compat.BuildCompat;\n"
        "import top.niunaijun.blackbox.utils.compat.FrameworkTypeAdapter;\n"
        "import top.niunaijun.blackbox.utils.compat.LegacyPermissionCompat;\n"
        "import top.niunaijun.blackbox.utils.compat.ParceledListSliceCompat;",
        "import permission translator",
    )

    old_flags = '''                if (packageInfo.requestedPermissions != null && packageInfo.requestedPermissionsFlags != null) {
                    for (int i = 0; i < packageInfo.requestedPermissions.length; i++) {
                        String perm = packageInfo.requestedPermissions[i];
                        if (perm != null && (perm.equals(android.Manifest.permission.RECORD_AUDIO)
                                || perm.equals("android.permission.FOREGROUND_SERVICE_MICROPHONE")
                                || perm.equals(android.Manifest.permission.MODIFY_AUDIO_SETTINGS)
                                || perm.equals(android.Manifest.permission.CAPTURE_AUDIO_OUTPUT))) {
                            packageInfo.requestedPermissionsFlags[i] |= PackageInfo.REQUESTED_PERMISSION_GRANTED;
                        }
                    }
                }'''
    new_flags = '''                if (packageInfo.requestedPermissions != null && packageInfo.requestedPermissionsFlags != null) {
                    for (int i = 0; i < packageInfo.requestedPermissions.length; i++) {
                        String perm = packageInfo.requestedPermissions[i];
                        Integer state = LegacyPermissionCompat.checkGuestPermission(perm, packageName);
                        if (state != null && state == PackageManager.PERMISSION_GRANTED) {
                            packageInfo.requestedPermissionsFlags[i] |= PackageInfo.REQUESTED_PERMISSION_GRANTED;
                        } else if (state != null) {
                            packageInfo.requestedPermissionsFlags[i] &= ~PackageInfo.REQUESTED_PERMISSION_GRANTED;
                        }
                    }
                }'''
    text = replace_once(text, old_flags, new_flags, "translate requestedPermissionsFlags")

    marker = '''            String permission = (String) args[0];
            String packageName = (String) args[1];
            
            
            if (isAudioPermission(permission)) {'''
    replacement = '''            String permission = (String) args[0];
            String packageName = (String) args[1];

            Integer translatedPermission = LegacyPermissionCompat.checkGuestPermission(permission, packageName);
            if (translatedPermission != null) {
                Slog.d(TAG, "Permission translation: " + permission + " for " + packageName
                        + " -> " + translatedPermission);
                return translatedPermission;
            }
            
            
            if (isAudioPermission(permission)) {'''
    # Same prefix exists in checkPermission and checkSelfPermission.
    count = text.count(marker)
    if count != 2:
        raise SystemExit(f"[permission-translation] expected two permission-check prefixes, found {count}")
    text = text.replace(marker, replacement, 2)
    print("[permission-translation] package/self permission checks: applied")

    rationale_old = '''            String permission = (String) args[0];
            String packageName = (String) args[1];
            
            
            if (isAudioPermission(permission)) {'''
    rationale_new = '''            String permission = (String) args[0];
            String packageName = (String) args[1];

            if (LegacyPermissionCompat.handlesGuestPermission(permission, packageName)) {
                return false;
            }
            
            
            if (isAudioPermission(permission)) {'''
    # After the two replacements above, exactly one original prefix remains: rationale.
    text = replace_once(text, rationale_old, rationale_new, "translate permission rationale")

    request_old = '''            String[] permissions = (String[]) args[0];
            String packageName = (String) args[1];
            
            
            
            if (permissions != null) {
                Slog.d(TAG, "RequestPermissions: Allowing permission request flow for: " + java.util.Arrays.toString(permissions));
            }
            
            return method.invoke(who, args);'''
    request_new = '''            String[] permissions = (String[]) args[0];
            String packageName = (String) args[1];

            // The virtual guest is not a package known to Android's real permission
            // controller. EngineBridgeActivity requests translated permissions for
            // the helper UID before guest startup. Do not forward a guest package
            // name into PackageManager where it can crash an onboarding flow.
            if (packageName != null
                    && BlackBoxCore.get().isInstalled(packageName, BlackBoxCore.getUserId())) {
                Slog.d(TAG, "RequestPermissions: guest request handled by AppCompat bridge: "
                        + java.util.Arrays.toString(permissions));
                return FrameworkTypeAdapter.adaptReturnValue(who, method, args, null);
            }

            return method.invoke(who, args);'''
    text = replace_once(text, request_old, request_new, "stop forwarding virtual runtime permission requests")

    path.write_text(text, encoding="utf-8")


def patch_activity_manager(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IActivityManagerProxy.java"
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "import top.niunaijun.blackbox.utils.compat.BuildCompat;\n"
        "import top.niunaijun.blackbox.utils.compat.ParceledListSliceCompat;",
        "import top.niunaijun.blackbox.utils.compat.BuildCompat;\n"
        "import top.niunaijun.blackbox.utils.compat.LegacyPermissionCompat;\n"
        "import top.niunaijun.blackbox.utils.compat.ParceledListSliceCompat;",
        "import ActivityManager permission translator",
    )

    old = '''            MethodParameterUtils.replaceLastUid(args);
            String permission = (String) args[0];
            if (permission.equals(Manifest.permission.ACCOUNT_MANAGER)
                    || permission.equals(Manifest.permission.SEND_SMS)) {
                return PackageManager.PERMISSION_GRANTED;
            }'''
    new = '''            MethodParameterUtils.replaceLastUid(args);
            String permission = (String) args[0];
            Integer translatedPermission = LegacyPermissionCompat.checkGuestPermission(
                    permission, BActivityThread.getAppPackageName());
            if (translatedPermission != null) {
                return translatedPermission;
            }
            if (permission.equals(Manifest.permission.ACCOUNT_MANAGER)
                    || permission.equals(Manifest.permission.SEND_SMS)) {
                return PackageManager.PERMISSION_GRANTED;
            }'''
    text = replace_once(text, old, new, "translate ActivityManager permission checks")
    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    compat = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyPermissionCompat.java"
    pm = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IPackageManagerProxy.java").read_text(encoding="utf-8")
    am = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IActivityManagerProxy.java").read_text(encoding="utf-8")
    if not compat.is_file():
        raise SystemExit("[permission-translation] LegacyPermissionCompat missing")
    for invariant in [
        "requiredHostRuntimePermissions",
        "READ_MEDIA_IMAGES",
        "BLUETOOTH_CONNECT",
        "checkGuestPermission",
    ]:
        if invariant not in compat.read_text(encoding="utf-8"):
            raise SystemExit(f"[permission-translation] compat invariant missing: {invariant}")
    for invariant in [
        "LegacyPermissionCompat.checkGuestPermission",
        "FrameworkTypeAdapter.adaptReturnValue",
        "guest request handled by AppCompat bridge",
    ]:
        if invariant not in pm:
            raise SystemExit(f"[permission-translation] PackageManager invariant missing: {invariant}")
    if "LegacyPermissionCompat.checkGuestPermission" not in am:
        raise SystemExit("[permission-translation] ActivityManager permission bridge missing")
    print("[permission-translation] modern permission translation verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_permission_translation.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    write_permission_compat(root)
    patch_package_manager(root)
    patch_activity_manager(root)
    verify(root)


if __name__ == "__main__":
    main()
