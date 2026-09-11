#!/usr/bin/env python3
"""Bridge Android-visible system components for virtual legacy guests.

Notification access is not the only permission backed by a real installed component.
Old utilities frequently depend on AccessibilityService and DeviceAdminReceiver too.
A virtual component cannot appear in Android Settings or DevicePolicyManager, so the
helper owns narrowly-scoped real proxy components while the guest keeps seeing its
original ComponentName and Settings.Secure state.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[system-component] {label}: already applied")
            return text
        raise SystemExit(f"[system-component] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[system-component] {label}: expected one match, found {count}")
    print(f"[system-component] {label}: applied")
    return text.replace(old, new, 1)


def write_compat(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacySystemComponentCompat.java"
    source = r'''package top.niunaijun.blackbox.utils.compat;

import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.provider.Settings;

import java.io.File;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import top.niunaijun.blackbox.BlackBoxCore;
import top.niunaijun.blackbox.app.BActivityThread;

/** Android-visible component translation for virtual Accessibility/DeviceAdmin apps. */
public final class LegacySystemComponentCompat {
    private static final String ACCESSIBILITY_PERMISSION =
            "android.permission.BIND_ACCESSIBILITY_SERVICE";
    private static final String ACCESSIBILITY_PROXY =
            "com.appcompat.engine.LegacyAccessibilityProxyService";
    private static final String DEVICE_ADMIN_PROXY =
            "com.appcompat.engine.LegacyDeviceAdminReceiver";

    public static final String ACTION_ACCESSIBILITY_EVENT =
            "com.appcompat.engine.component.ACCESSIBILITY_EVENT";
    public static final String ACTION_ACCESSIBILITY_CONNECTED =
            "com.appcompat.engine.component.ACCESSIBILITY_CONNECTED";
    public static final String ACTION_ACCESSIBILITY_INTERRUPT =
            "com.appcompat.engine.component.ACCESSIBILITY_INTERRUPT";
    public static final String EXTRA_ACCESSIBILITY_EVENT =
            "com.appcompat.engine.component.ACCESSIBILITY_EVENT_EXTRA";

    private LegacySystemComponentCompat() {
    }

    /** Mutates real-system grant intents while retaining guest-side logical identity. */
    public static boolean rewriteSystemGrantIntent(Intent intent, String guestPackage) {
        if (intent == null || !isSafePackageName(guestPackage)) return false;
        String action = intent.getAction();
        if (Settings.ACTION_ACCESSIBILITY_SETTINGS.equals(action)) {
            markAccessibilityRequested(guestPackage);
            intent.setComponent(null);
            intent.setPackage(null);
            intent.setData(null);
            return true;
        }

        if (DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN.equals(action)) {
            ComponentName guestAdmin = null;
            try {
                if (Build.VERSION.SDK_INT >= 33) {
                    guestAdmin = intent.getParcelableExtra(
                            DevicePolicyManager.EXTRA_DEVICE_ADMIN, ComponentName.class);
                } else {
                    guestAdmin = intent.getParcelableExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN);
                }
            } catch (Throwable ignored) {
            }
            if (guestAdmin != null && isGuestComponent(guestAdmin)) {
                markDeviceAdminRequested(guestAdmin);
                ComponentName proxy = deviceAdminProxyComponent();
                intent.putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, proxy);
                intent.setComponent(null);
                intent.setPackage(null);
                intent.setData(null);
                return true;
            }
        }
        return false;
    }

    public static boolean isSystemGrantIntent(Intent intent) {
        if (intent == null) return false;
        String action = intent.getAction();
        return Settings.ACTION_ACCESSIBILITY_SETTINGS.equals(action)
                || DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN.equals(action);
    }

    /** OEM-safe fallback for the two Android-controlled grant UIs. */
    public static void ensureResolvableSystemGrantIntent(Intent intent) {
        if (!isSystemGrantIntent(intent)) return;
        try {
            Context context = BlackBoxCore.getContext();
            PackageManager pm = context.getPackageManager();
            if (pm.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY) != null) return;

            String action = intent.getAction();
            intent.setComponent(null);
            intent.setPackage(null);
            intent.setData(null);
            if (pm.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY) != null) return;

            // Device-admin has no equivalent generic fallback that could safely
            // preserve consent semantics; the Security settings root is the closest
            // truthful surface. Accessibility has its own stable list action.
            if (DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN.equals(action)) {
                intent.setAction(Settings.ACTION_SECURITY_SETTINGS);
                intent.removeExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN);
            } else {
                intent.setAction(Settings.ACTION_ACCESSIBILITY_SETTINGS);
            }
        } catch (Throwable ignored) {
        }
    }

    public static String translatedSecureString(Object[] args) {
        String key = firstStringKey(args);
        if (key == null) return null;
        String guestPackage = BActivityThread.getAppPackageName();
        if (!isSafePackageName(guestPackage)) return null;

        if (Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES.equalsIgnoreCase(key)) {
            if (!isAccessibilityRequested(guestPackage) || !isHostAccessibilityEnabled()) {
                return "";
            }
            StringBuilder out = new StringBuilder();
            for (ComponentName component : accessibilityComponents(guestPackage)) {
                if (out.length() > 0) out.append(':');
                out.append(component.flattenToString());
            }
            return out.toString();
        }
        if (Settings.Secure.ACCESSIBILITY_ENABLED.equalsIgnoreCase(key)) {
            return isAccessibilityRequested(guestPackage) && isHostAccessibilityEnabled()
                    ? "1" : "0";
        }
        return null;
    }

    public static Integer translatedSecureInt(Object[] args) {
        String key = firstStringKey(args);
        if (key == null || !Settings.Secure.ACCESSIBILITY_ENABLED.equalsIgnoreCase(key)) {
            return null;
        }
        String guestPackage = BActivityThread.getAppPackageName();
        return isSafePackageName(guestPackage)
                && isAccessibilityRequested(guestPackage)
                && isHostAccessibilityEnabled() ? 1 : 0;
    }

    /** Guests that opted in and can therefore receive real proxy callbacks. */
    public static List<ComponentName> accessibilityGuestComponents() {
        if (!isHostAccessibilityEnabled()) return Collections.emptyList();
        File[] markers = accessibilityMarkerDir().listFiles();
        if (markers == null || markers.length == 0) return Collections.emptyList();
        ArrayList<ComponentName> out = new ArrayList<>();
        for (File marker : markers) {
            String name = marker.getName();
            if (!name.endsWith(".enabled")) continue;
            String pkg = name.substring(0, name.length() - ".enabled".length());
            if (isSafePackageName(pkg)) out.addAll(accessibilityComponents(pkg));
        }
        return out;
    }

    public static boolean isHostAccessibilityEnabled() {
        try {
            Context context = BlackBoxCore.getContext();
            if (Settings.Secure.getInt(
                    context.getContentResolver(), Settings.Secure.ACCESSIBILITY_ENABLED, 0) == 0) {
                return false;
            }
            String enabled = Settings.Secure.getString(
                    context.getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
            if (enabled == null || enabled.isEmpty()) return false;
            ComponentName proxy = new ComponentName(context.getPackageName(), ACCESSIBILITY_PROXY);
            String flat = proxy.flattenToString();
            String shortFlat = proxy.flattenToShortString();
            for (String item : enabled.split(":")) {
                if (flat.equals(item) || shortFlat.equals(item)) return true;
            }
        } catch (Throwable ignored) {
        }
        return false;
    }

    public static boolean isDeviceAdminActiveForGuest(ComponentName guestAdmin) {
        if (guestAdmin == null || !isGuestComponent(guestAdmin)) return false;
        ComponentName requested = requestedDeviceAdmin(guestAdmin.getPackageName());
        if (requested == null || !requested.equals(guestAdmin)) return false;
        return isHostDeviceAdminActive();
    }

    public static List<ComponentName> deviceAdminGuestComponents() {
        if (!isHostDeviceAdminActive()) return Collections.emptyList();
        File[] markers = deviceAdminMarkerDir().listFiles();
        if (markers == null || markers.length == 0) return Collections.emptyList();
        ArrayList<ComponentName> out = new ArrayList<>();
        for (File marker : markers) {
            if (!marker.getName().endsWith(".admin")) continue;
            try {
                ComponentName component = ComponentName.unflattenFromString(markerText(marker));
                if (component != null && isGuestComponent(component)) out.add(component);
            } catch (Throwable ignored) {
            }
        }
        return out;
    }

    public static ComponentName deviceAdminProxyComponent() {
        Context context = BlackBoxCore.getContext();
        return new ComponentName(context.getPackageName(), DEVICE_ADMIN_PROXY);
    }

    public static ComponentName translateDeviceAdminComponent(ComponentName component) {
        if (component != null && isDeviceAdminRequested(component)) {
            return deviceAdminProxyComponent();
        }
        return component;
    }

    public static void clearDeviceAdminRequested(ComponentName guestAdmin) {
        if (guestAdmin == null || !isSafePackageName(guestAdmin.getPackageName())) return;
        try {
            new File(deviceAdminMarkerDir(), guestAdmin.getPackageName() + ".admin").delete();
        } catch (Throwable ignored) {
        }
    }

    public static void clearGuestState(String guestPackage) {
        if (!isSafePackageName(guestPackage)) return;
        try { new File(accessibilityMarkerDir(), guestPackage + ".enabled").delete(); } catch (Throwable ignored) {}
        try { new File(deviceAdminMarkerDir(), guestPackage + ".admin").delete(); } catch (Throwable ignored) {}
    }

    public static boolean isGuestComponent(ComponentName component) {
        if (component == null) return false;
        try {
            return BlackBoxCore.get().isInstalled(component.getPackageName(), BlackBoxCore.getUserId());
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static void markAccessibilityRequested(String guestPackage) {
        if (!isSafePackageName(guestPackage)) return;
        try {
            File dir = accessibilityMarkerDir();
            if (!dir.isDirectory() && !dir.mkdirs()) return;
            new File(dir, guestPackage + ".enabled").createNewFile();
        } catch (Throwable ignored) {
        }
    }

    private static boolean isAccessibilityRequested(String guestPackage) {
        if (!isSafePackageName(guestPackage)) return false;
        try {
            return new File(accessibilityMarkerDir(), guestPackage + ".enabled").isFile();
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static void markDeviceAdminRequested(ComponentName guestAdmin) {
        if (guestAdmin == null || !isSafePackageName(guestAdmin.getPackageName())) return;
        try {
            File dir = deviceAdminMarkerDir();
            if (!dir.isDirectory() && !dir.mkdirs()) return;
            File marker = new File(dir, guestAdmin.getPackageName() + ".admin");
            java.io.FileOutputStream output = new java.io.FileOutputStream(marker, false);
            try {
                output.write(guestAdmin.flattenToString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
            } finally {
                output.close();
            }
        } catch (Throwable ignored) {
        }
    }

    private static boolean isDeviceAdminRequested(ComponentName guestAdmin) {
        if (guestAdmin == null || !isSafePackageName(guestAdmin.getPackageName())) return false;
        ComponentName requested = requestedDeviceAdmin(guestAdmin.getPackageName());
        return requested != null && requested.equals(guestAdmin);
    }

    private static ComponentName requestedDeviceAdmin(String guestPackage) {
        if (!isSafePackageName(guestPackage)) return null;
        try {
            File marker = new File(deviceAdminMarkerDir(), guestPackage + ".admin");
            if (!marker.isFile()) return null;
            return ComponentName.unflattenFromString(markerText(marker));
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static boolean isHostDeviceAdminActive() {
        try {
            Context context = BlackBoxCore.getContext();
            DevicePolicyManager dpm =
                    (DevicePolicyManager) context.getSystemService(Context.DEVICE_POLICY_SERVICE);
            return dpm != null && dpm.isAdminActive(deviceAdminProxyComponent());
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static List<ComponentName> accessibilityComponents(String guestPackage) {
        if (!isSafePackageName(guestPackage)) return Collections.emptyList();
        ArrayList<ComponentName> out = new ArrayList<>();
        try {
            PackageInfo info = BlackBoxCore.getBPackageManager().getPackageInfo(
                    guestPackage, PackageManager.GET_SERVICES, BlackBoxCore.getUserId());
            if (info == null || info.services == null) return out;
            for (ServiceInfo service : info.services) {
                if (service == null || service.name == null) continue;
                if (ACCESSIBILITY_PERMISSION.equals(service.permission)) {
                    out.add(new ComponentName(guestPackage, service.name));
                }
            }
        } catch (Throwable ignored) {
        }
        return out;
    }

    private static String firstStringKey(Object[] args) {
        if (args == null) return null;
        for (Object arg : args) if (arg instanceof String) return (String) arg;
        return null;
    }

    private static String markerText(File file) throws java.io.IOException {
        java.io.FileInputStream input = new java.io.FileInputStream(file);
        try {
            java.io.ByteArrayOutputStream output = new java.io.ByteArrayOutputStream();
            byte[] buffer = new byte[256];
            int read;
            while ((read = input.read(buffer)) >= 0) output.write(buffer, 0, read);
            return new String(output.toByteArray(), java.nio.charset.StandardCharsets.UTF_8).trim();
        } finally {
            input.close();
        }
    }

    private static File accessibilityMarkerDir() {
        return new File(BlackBoxCore.getContext().getFilesDir(),
                "appcompat-special-access/accessibility");
    }

    private static File deviceAdminMarkerDir() {
        return new File(BlackBoxCore.getContext().getFilesDir(),
                "appcompat-special-access/device-admin");
    }

    private static boolean isSafePackageName(String value) {
        return value != null && !value.isEmpty()
                && value.matches("[A-Za-z0-9_]+(?:\\.[A-Za-z0-9_]+)+");
    }
}
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    print("[system-component] LegacySystemComponentCompat: written")


def patch_activity_manager(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/ActivityManagerCommonProxy.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;\nimport top.niunaijun.blackbox.utils.compat.StartActivityCompat;",
        "import top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;\nimport top.niunaijun.blackbox.utils.compat.LegacySystemComponentCompat;\nimport top.niunaijun.blackbox.utils.compat.StartActivityCompat;",
        "import system component translator",
    )
    old = '''            LegacySpecialAccessCompat.rewriteSettingsIntent(
                    intent, BActivityThread.getAppPackageName());
            LegacySpecialAccessCompat.ensureResolvableSpecialAccessSettingsIntent(intent);'''
    new = '''            LegacySpecialAccessCompat.rewriteSettingsIntent(
                    intent, BActivityThread.getAppPackageName());
            LegacySpecialAccessCompat.ensureResolvableSpecialAccessSettingsIntent(intent);
            LegacySystemComponentCompat.rewriteSystemGrantIntent(
                    intent, BActivityThread.getAppPackageName());
            LegacySystemComponentCompat.ensureResolvableSystemGrantIntent(intent);'''
    text = replace_once(text, old, new, "rewrite Accessibility/DeviceAdmin grant intents")

    old_guard = '''            boolean specialAccessSettings =
                    LegacySpecialAccessCompat.isSpecialAccessSettingsIntent(intent);
            if (!specialAccessSettings
                    && !AppSystemEnv.isOpenPackage(hostResolve.activityInfo.packageName)) {'''
    new_guard = '''            boolean specialAccessSettings =
                    LegacySpecialAccessCompat.isSpecialAccessSettingsIntent(intent);
            boolean systemComponentGrant =
                    LegacySystemComponentCompat.isSystemGrantIntent(intent);
            if (!specialAccessSettings && !systemComponentGrant
                    && !AppSystemEnv.isOpenPackage(hostResolve.activityInfo.packageName)) {'''
    text = replace_once(text, old_guard, new_guard, "allow translated component grant UI")

    text = replace_once(
        text,
        '''            if (requestCode >= 0 && !didRedirectSamsungAccount && !specialAccessSettings) {
                return;
            }
            if (requestCode >= 0 && specialAccessSettings) {''',
        '''            if (requestCode >= 0 && !didRedirectSamsungAccount
                    && !specialAccessSettings && !systemComponentGrant) {
                return;
            }
            if (requestCode >= 0 && (specialAccessSettings || systemComponentGrant)) {''',
        "strip invalid virtual result token for component grant UI",
    )
    path.write_text(text, encoding="utf-8")


def patch_settings_provider(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/ISettingsProviderProxy.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;",
        "import top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;\nimport top.niunaijun.blackbox.utils.compat.LegacySystemComponentCompat;",
        "import accessibility secure-state translator",
    )

    old_string = '''                String specialAccessValue = LegacySpecialAccessCompat.translatedSecureString(args);
                if (specialAccessValue != null) {
                    return specialAccessValue;
                }
                String spoofedAndroidId = getSpoofedAndroidId(args);'''
    new_string = '''                String specialAccessValue = LegacySpecialAccessCompat.translatedSecureString(args);
                if (specialAccessValue != null) {
                    return specialAccessValue;
                }
                String systemComponentValue = LegacySystemComponentCompat.translatedSecureString(args);
                if (systemComponentValue != null) {
                    return systemComponentValue;
                }
                String spoofedAndroidId = getSpoofedAndroidId(args);'''
    count = text.count(old_string)
    if count != 2:
        raise SystemExit(f"[system-component] expected two translated string Settings hooks, found {count}")
    text = text.replace(old_string, new_string, 2)
    print("[system-component] synthesize guest accessibility Settings strings: applied")

    old_int = '''            try {
                if (args != null && args.length > 0 && args[0] instanceof String) {'''
    new_int = '''            try {
                Integer componentValue = LegacySystemComponentCompat.translatedSecureInt(args);
                if (componentValue != null) {
                    return componentValue;
                }
                if (args != null && args.length > 0 && args[0] instanceof String) {'''
    count = text.count(old_int)
    if count != 2:
        raise SystemExit(f"[system-component] expected two integer Settings hooks, found {count}")
    text = text.replace(old_int, new_int, 2)
    print("[system-component] synthesize guest accessibility Settings integer: applied")
    path.write_text(text, encoding="utf-8")


def patch_service_dispatcher(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/app/dispatcher/AppServiceDispatcher.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import android.os.IBinder;\nimport android.service.notification.NotificationListenerService;",
        "import android.os.IBinder;\nimport android.accessibilityservice.AccessibilityService;\nimport android.view.accessibility.AccessibilityEvent;\nimport android.service.notification.NotificationListenerService;",
        "import accessibility callback types",
    )
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;",
        "import top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;\nimport top.niunaijun.blackbox.utils.compat.LegacySystemComponentCompat;",
        "import system component callback bridge",
    )
    text = replace_once(
        text,
        '''        try {
            int bridgeResult = dispatchSpecialAccessCallback(service, stubRecord.mServiceIntent);''',
        '''        try {
            int componentBridgeResult = dispatchSystemComponentCallback(service, stubRecord.mServiceIntent);
            if (componentBridgeResult != Integer.MIN_VALUE) {
                BlackBoxCore.getBActivityManager().onStartCommand(proxyIntent, stubRecord.mUserId);
                return componentBridgeResult;
            }
            int bridgeResult = dispatchSpecialAccessCallback(service, stubRecord.mServiceIntent);''',
        "dispatch accessibility callbacks before onStartCommand",
    )

    marker = '''    /**
     * A virtual NotificationListenerService cannot be bound by Android directly.'''
    method = r'''    /** Forward the real AccessibilityService proxy callbacks into a virtual service. */
    private int dispatchSystemComponentCallback(Service service, Intent intent) {
        if (!(service instanceof AccessibilityService) || intent == null) {
            return Integer.MIN_VALUE;
        }
        String action = intent.getAction();
        if (action == null || !action.startsWith("com.appcompat.engine.component.ACCESSIBILITY_")) {
            return Integer.MIN_VALUE;
        }
        AccessibilityService accessibility = (AccessibilityService) service;
        try {
            if (LegacySystemComponentCompat.ACTION_ACCESSIBILITY_CONNECTED.equals(action)) {
                try {
                    java.lang.reflect.Method callback =
                            AccessibilityService.class.getDeclaredMethod("onServiceConnected");
                    callback.setAccessible(true);
                    callback.invoke(accessibility);
                } catch (Throwable ignored) {
                    // The callback is optional for legacy services; event delivery is
                    // still more useful than killing the guest on reflective drift.
                }
                return START_NOT_STICKY;
            }
            if (LegacySystemComponentCompat.ACTION_ACCESSIBILITY_INTERRUPT.equals(action)) {
                accessibility.onInterrupt();
                return START_NOT_STICKY;
            }
            AccessibilityEvent event;
            if (android.os.Build.VERSION.SDK_INT >= 33) {
                event = intent.getParcelableExtra(
                        LegacySystemComponentCompat.EXTRA_ACCESSIBILITY_EVENT,
                        AccessibilityEvent.class);
            } else {
                event = intent.getParcelableExtra(
                        LegacySystemComponentCompat.EXTRA_ACCESSIBILITY_EVENT);
            }
            if (event != null
                    && LegacySystemComponentCompat.ACTION_ACCESSIBILITY_EVENT.equals(action)) {
                accessibility.onAccessibilityEvent(event);
                return START_NOT_STICKY;
            }
            return START_NOT_STICKY;
        } catch (Throwable callbackFailure) {
            callbackFailure.printStackTrace();
            return START_NOT_STICKY;
        }
    }

'''
    if "dispatchSystemComponentCallback(Service service" not in text:
        if marker not in text:
            raise SystemExit("[system-component] dispatcher insertion marker not found")
        text = text.replace(marker, method + marker, 1)
        print("[system-component] accessibility service callback dispatcher: applied")
    path.write_text(text, encoding="utf-8")


def patch_device_policy(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IDevicePolicyManagerProxy.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.utils.MethodParameterUtils;",
        "import top.niunaijun.blackbox.utils.MethodParameterUtils;\nimport top.niunaijun.blackbox.utils.compat.LegacySystemComponentCompat;",
        "import device-admin component translator",
    )

    insertion = r'''

    @ProxyMethod("isAdminActive")
    public static class IsAdminActive extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            ComponentName component = MethodParameterUtils.getFirstParam(args, ComponentName.class);
            if (LegacySystemComponentCompat.isGuestComponent(component)) {
                return LegacySystemComponentCompat.isDeviceAdminActiveForGuest(component);
            }
            return method.invoke(who, args);
        }
    }

    @ProxyMethod("getActiveAdmins")
    public static class GetActiveAdmins extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            java.util.List<ComponentName> guests =
                    LegacySystemComponentCompat.deviceAdminGuestComponents();
            if (!guests.isEmpty()) return guests;
            return method.invoke(who, args);
        }
    }

    @ProxyMethod("hasGrantedPolicy")
    public static class HasGrantedPolicy extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            ComponentName component = MethodParameterUtils.getFirstParam(args, ComponentName.class);
            if (LegacySystemComponentCompat.isGuestComponent(component)) {
                replaceComponent(args, component,
                        LegacySystemComponentCompat.deviceAdminProxyComponent());
            }
            return method.invoke(who, args);
        }
    }

    @ProxyMethod("removeActiveAdmin")
    public static class RemoveActiveAdmin extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            ComponentName component = MethodParameterUtils.getFirstParam(args, ComponentName.class);
            if (LegacySystemComponentCompat.isGuestComponent(component)) {
                replaceComponent(args, component,
                        LegacySystemComponentCompat.deviceAdminProxyComponent());
                Object result = method.invoke(who, args);
                LegacySystemComponentCompat.clearDeviceAdminRequested(component);
                return result;
            }
            return method.invoke(who, args);
        }
    }

    private static void replaceComponent(
            Object[] args, ComponentName from, ComponentName to) {
        if (args == null || from == null || to == null) return;
        for (int i = 0; i < args.length; i++) {
            if (from.equals(args[i])) args[i] = to;
        }
    }
'''
    if "class IsAdminActive" not in text:
        pos = text.rfind("\n}")
        if pos < 0:
            raise SystemExit("[system-component] DevicePolicyManager closing brace not found")
        text = text[:pos] + insertion + text[pos:]
        print("[system-component] DevicePolicyManager guest identity hooks: applied")
    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    compat = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacySystemComponentCompat.java"
    activity = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/ActivityManagerCommonProxy.java").read_text(encoding="utf-8")
    settings = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/ISettingsProviderProxy.java").read_text(encoding="utf-8")
    dispatcher = (root / "Bcore/src/main/java/top/niunaijun/blackbox/app/dispatcher/AppServiceDispatcher.java").read_text(encoding="utf-8")
    dpm = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IDevicePolicyManagerProxy.java").read_text(encoding="utf-8")
    if not compat.is_file():
        raise SystemExit("[system-component] compat source missing")
    for content, invariant in [
        (activity, "rewriteSystemGrantIntent"),
        (activity, "systemComponentGrant"),
        (settings, "LegacySystemComponentCompat.translatedSecureString"),
        (settings, "LegacySystemComponentCompat.translatedSecureInt"),
        (dispatcher, "dispatchSystemComponentCallback"),
        (dpm, "isDeviceAdminActiveForGuest"),
        (dpm, "deviceAdminProxyComponent"),
    ]:
        if invariant not in content:
            raise SystemExit(f"[system-component] verification failed: {invariant}")
    print("[system-component] accessibility + device-admin translation verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_system_component_translation.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    write_compat(root)
    patch_activity_manager(root)
    patch_settings_provider(root)
    patch_service_dispatcher(root)
    patch_device_policy(root)
    verify(root)


if __name__ == "__main__":
    main()
