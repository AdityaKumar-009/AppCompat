#!/usr/bin/env python3
"""Patch the pinned engine with special-app-access translation.

Virtual packages are not real Android packages, so Settings cannot list them for
notification listener, overlay, usage access, write settings, exact alarms, and
other Special app access pages. This bridge makes the ABI helper the real Android
permission principal while preserving the guest's logical package/component view.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[special-access] {label}: already applied")
            return text
        raise SystemExit(f"[special-access] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[special-access] {label}: expected one match, found {count}")
    print(f"[special-access] {label}: applied")
    return text.replace(old, new, 1)


def write_compat(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacySpecialAccessCompat.java"
    source = r'''package top.niunaijun.blackbox.utils.compat;

import android.app.AppOpsManager;
import android.app.NotificationManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import java.io.File;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

import top.niunaijun.blackbox.BlackBoxCore;
import top.niunaijun.blackbox.app.BActivityThread;

/**
 * Translates Android "Special app access" from a virtual guest identity onto the
 * real AppCompat helper package. Runtime permissions are handled separately by
 * LegacyPermissionCompat; this class covers Settings/AppOps/component grants.
 */
public final class LegacySpecialAccessCompat {
    private static final String NOTIFICATION_LISTENER_PERMISSION =
            "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE";
    private static final String NOTIFICATION_LISTENER_PROXY =
            "com.appcompat.engine.NotificationAccessProxyService";
    private static final String NOTIFICATION_LISTENER_SETTINGS =
            "android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS";
    private static final String NOTIFICATION_LISTENER_DETAIL_SETTINGS =
            "android.settings.NOTIFICATION_LISTENER_DETAIL_SETTINGS";
    private static final String EXTRA_NOTIFICATION_LISTENER_COMPONENT =
            "android.provider.extra.NOTIFICATION_LISTENER_COMPONENT_NAME";
    private static final String ENABLED_NOTIFICATION_LISTENERS =
            "enabled_notification_listeners";

    public static final String ACTION_NOTIFICATION_POSTED =
            "com.appcompat.engine.special.NOTIFICATION_POSTED";
    public static final String ACTION_NOTIFICATION_REMOVED =
            "com.appcompat.engine.special.NOTIFICATION_REMOVED";
    public static final String ACTION_NOTIFICATION_LISTENER_CONNECTED =
            "com.appcompat.engine.special.NOTIFICATION_LISTENER_CONNECTED";
    public static final String ACTION_NOTIFICATION_LISTENER_DISCONNECTED =
            "com.appcompat.engine.special.NOTIFICATION_LISTENER_DISCONNECTED";
    public static final String EXTRA_STATUS_BAR_NOTIFICATION =
            "com.appcompat.engine.special.STATUS_BAR_NOTIFICATION";

    private static final Set<String> PACKAGE_SCOPED_SETTINGS_ACTIONS;
    static {
        LinkedHashSet<String> actions = new LinkedHashSet<>();
        actions.add("android.settings.action.MANAGE_OVERLAY_PERMISSION");
        actions.add("android.settings.action.MANAGE_WRITE_SETTINGS");
        actions.add("android.settings.MANAGE_UNKNOWN_APP_SOURCES");
        actions.add("android.settings.MANAGE_APP_ALL_FILES_ACCESS_PERMISSION");
        actions.add("android.settings.REQUEST_SCHEDULE_EXACT_ALARM");
        actions.add("android.settings.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS");
        actions.add("android.settings.PICTURE_IN_PICTURE_SETTINGS");
        actions.add("android.settings.MANAGE_APP_USE_FULL_SCREEN_INTENT");
        actions.add(Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
        PACKAGE_SCOPED_SETTINGS_ACTIONS = Collections.unmodifiableSet(actions);
    }

    private LegacySpecialAccessCompat() {
    }

    /** Mutates the outgoing Settings intent so Android sees the real helper. */
    public static Intent rewriteSettingsIntent(Intent intent, String guestPackage) {
        if (intent == null || guestPackage == null || guestPackage.isEmpty()) {
            return intent;
        }
        String hostPackage = BlackBoxCore.getHostPkg();
        String action = intent.getAction();

        Uri data = intent.getData();
        if (data != null && "package".equals(data.getScheme())
                && guestPackage.equals(data.getSchemeSpecificPart())) {
            intent.setData(Uri.fromParts("package", hostPackage, null));
        }

        // Notification listener access is component-based rather than package-based.
        // Make the real proxy service the component Android grants, then synthesize
        // the virtual service identity back to the guest on permission checks.
        if (NOTIFICATION_LISTENER_SETTINGS.equals(action)
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
        }

        if (action != null && PACKAGE_SCOPED_SETTINGS_ACTIONS.contains(action)) {
            intent.setData(Uri.fromParts("package", hostPackage, null));
        }

        // These pages are list-style on AOSP. Declaring the matching special
        // permission on the helper makes AppCompat Runtime appear as the real app.
        // OEMs that honor package: data can deep-link directly; others ignore it.
        if ("android.settings.USAGE_ACCESS_SETTINGS".equals(action)
                || "android.settings.NOTIFICATION_POLICY_ACCESS_SETTINGS".equals(action)
                || "android.settings.IGNORE_BATTERY_OPTIMIZATION_SETTINGS".equals(action)) {
            intent.setData(Uri.fromParts("package", hostPackage, null));
        }

        // Modern app-notification Settings uses extras instead of only data URI.
        if ("android.settings.APP_NOTIFICATION_SETTINGS".equals(action)) {
            intent.putExtra(Settings.EXTRA_APP_PACKAGE, hostPackage);
            intent.putExtra("app_package", hostPackage);
            intent.putExtra("android.provider.extra.APP_PACKAGE", hostPackage);
        }
        return intent;
    }

    public static void markNotificationListenerRequested(String guestPackage) {
        if (!isSafePackageName(guestPackage)) return;
        try {
            File dir = notificationListenerMarkerDir();
            if (!dir.isDirectory() && !dir.mkdirs()) return;
            File marker = new File(dir, guestPackage + ".enabled");
            if (!marker.exists()) marker.createNewFile();
        } catch (Throwable ignored) {
        }
    }

    public static void clearGuestSpecialAccess(String guestPackage) {
        if (!isSafePackageName(guestPackage)) return;
        try {
            new File(notificationListenerMarkerDir(), guestPackage + ".enabled").delete();
        } catch (Throwable ignored) {
        }
    }

    public static boolean isNotificationListenerAccessGrantedForGuest(String guestPackage) {
        return isNotificationListenerRequested(guestPackage) && isHostNotificationListenerGranted();
    }

    public static boolean isNotificationPolicyAccessGranted() {
        try {
            Context context = BlackBoxCore.getContext();
            NotificationManager manager =
                    (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            return manager != null && manager.isNotificationPolicyAccessGranted();
        } catch (Throwable ignored) {
            return false;
        }
    }

    /**
     * Settings.Secure is how legacy support libraries discover enabled listener
     * packages. Return the guest components only after the real helper grant exists.
     */
    public static String translatedSecureString(Object[] args) {
        if (args == null) return null;
        boolean notificationKey = false;
        for (Object arg : args) {
            if (arg instanceof String
                    && ENABLED_NOTIFICATION_LISTENERS.equalsIgnoreCase((String) arg)) {
                notificationKey = true;
                break;
            }
        }
        if (!notificationKey) return null;

        String guestPackage = BActivityThread.getAppPackageName();
        if (!isNotificationListenerAccessGrantedForGuest(guestPackage)) {
            return "";
        }
        List<ComponentName> components = notificationListenerComponents(guestPackage);
        if (components.isEmpty()) {
            // NotificationManagerCompat only needs a valid flattened component to
            // recover the package name; direct component checks are hooked too.
            return new ComponentName(guestPackage, guestPackage + ".AppCompatNotificationListener")
                    .flattenToString();
        }
        StringBuilder out = new StringBuilder();
        for (ComponentName component : components) {
            if (out.length() > 0) out.append(':');
            out.append(component.flattenToString());
        }
        return out.toString();
    }

    /** Components currently opted into the real proxy listener. */
    public static List<ComponentName> notificationListenerGuestComponents() {
        if (!isHostNotificationListenerGranted()) return Collections.emptyList();
        File[] markers;
        try {
            markers = notificationListenerMarkerDir().listFiles();
        } catch (Throwable ignored) {
            markers = null;
        }
        if (markers == null || markers.length == 0) return Collections.emptyList();

        ArrayList<ComponentName> out = new ArrayList<>();
        for (File marker : markers) {
            String name = marker.getName();
            if (!name.endsWith(".enabled")) continue;
            String guestPackage = name.substring(0, name.length() - ".enabled".length());
            if (!isSafePackageName(guestPackage)) continue;
            out.addAll(notificationListenerComponents(guestPackage));
        }
        return out;
    }

    public static boolean isGuestComponent(ComponentName component) {
        if (component == null) return false;
        try {
            return BlackBoxCore.get().isInstalled(component.getPackageName(), BlackBoxCore.getUserId());
        } catch (Throwable ignored) {
            return false;
        }
    }

    /**
     * AppOps for special access must be truthful. The older engine used to return
     * MODE_ALLOWED for every check, which let onboarding continue without Android
     * actually granting the capability and caused failures later at the real API.
     */
    public static boolean isSpecialAccessAppOp(Object[] args) {
        if (args == null || args.length == 0) return false;
        String opName = null;
        for (Object arg : args) {
            if (arg instanceof Integer) {
                opName = appOpName((Integer) arg);
                break;
            }
        }
        if (opName == null) {
            for (Object arg : args) {
                if (!(arg instanceof String)) continue;
                String candidate = ((String) arg).toUpperCase(Locale.ROOT);
                if (candidate.contains("SYSTEM_ALERT_WINDOW")
                        || candidate.contains("WRITE_SETTINGS")
                        || candidate.contains("GET_USAGE_STATS")
                        || candidate.contains("REQUEST_INSTALL_PACKAGES")
                        || candidate.contains("MANAGE_EXTERNAL_STORAGE")
                        || candidate.contains("SCHEDULE_EXACT_ALARM")
                        || candidate.contains("USE_FULL_SCREEN_INTENT")) {
                    opName = candidate;
                    break;
                }
            }
        }
        if (opName == null) return false;
        String n = opName.toUpperCase(Locale.ROOT);
        return n.contains("SYSTEM_ALERT_WINDOW")
                || n.contains("WRITE_SETTINGS")
                || n.contains("GET_USAGE_STATS")
                || n.contains("REQUEST_INSTALL_PACKAGES")
                || n.contains("MANAGE_EXTERNAL_STORAGE")
                || n.contains("SCHEDULE_EXACT_ALARM")
                || n.contains("USE_FULL_SCREEN_INTENT");
    }

    private static String appOpName(int op) {
        try {
            java.lang.reflect.Method method =
                    AppOpsManager.class.getMethod("opToPublicName", int.class);
            Object value = method.invoke(null, op);
            if (value != null) return value.toString();
        } catch (Throwable ignored) {
        }
        try {
            java.lang.reflect.Method method = AppOpsManager.class.getDeclaredMethod("opToName", int.class);
            method.setAccessible(true);
            Object value = method.invoke(null, op);
            return value == null ? null : value.toString();
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static boolean isHostNotificationListenerGranted() {
        try {
            Context context = BlackBoxCore.getContext();
            ComponentName proxy = new ComponentName(context, NOTIFICATION_LISTENER_PROXY);
            if (Build.VERSION.SDK_INT >= 27) {
                NotificationManager manager =
                        (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
                if (manager != null) {
                    return manager.isNotificationListenerAccessGranted(proxy);
                }
            }
            String enabled = Settings.Secure.getString(
                    context.getContentResolver(), ENABLED_NOTIFICATION_LISTENERS);
            if (enabled == null || enabled.isEmpty()) return false;
            String flattened = proxy.flattenToString();
            String shortFlattened = proxy.flattenToShortString();
            for (String item : enabled.split(":")) {
                if (flattened.equals(item) || shortFlattened.equals(item)) return true;
            }
        } catch (Throwable ignored) {
        }
        return false;
    }

    private static List<ComponentName> notificationListenerComponents(String guestPackage) {
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
    }

    private static boolean isNotificationListenerRequested(String guestPackage) {
        if (!isSafePackageName(guestPackage)) return false;
        try {
            return new File(notificationListenerMarkerDir(), guestPackage + ".enabled").isFile();
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static File notificationListenerMarkerDir() {
        return new File(BlackBoxCore.getContext().getFilesDir(),
                "appcompat-special-access/notification-listener");
    }

    private static boolean isSafePackageName(String value) {
        return value != null && !value.isEmpty()
                && value.matches("[A-Za-z0-9_]+(?:\\.[A-Za-z0-9_]+)+");
    }
}
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    print("[special-access] LegacySpecialAccessCompat: written")


def patch_start_activity(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/ActivityManagerCommonProxy.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.utils.compat.BuildCompat;\nimport top.niunaijun.blackbox.utils.compat.StartActivityCompat;",
        "import top.niunaijun.blackbox.utils.compat.BuildCompat;\nimport top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;\nimport top.niunaijun.blackbox.utils.compat.StartActivityCompat;",
        "import special access intent translator",
    )
    old = '''            Intent intent = getIntent(args);
            Slog.d(TAG, "Hook in : " + intent);
            assert intent != null;'''
    new = '''            Intent intent = getIntent(args);
            Slog.d(TAG, "Hook in : " + intent);
            assert intent != null;

            // A virtual package cannot appear in Android's Special app access UI.
            // Translate Settings intents to the real helper UID/component while
            // retaining the guest identity inside the compatibility runtime.
            LegacySpecialAccessCompat.rewriteSettingsIntent(
                    intent, BActivityThread.getAppPackageName());'''
    text = replace_once(text, old, new, "rewrite special access Settings intents")
    path.write_text(text, encoding="utf-8")


def patch_settings_provider(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/ISettingsProviderProxy.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.fake.hook.ProxyMethod;\nimport top.niunaijun.blackbox.utils.Slog;",
        "import top.niunaijun.blackbox.fake.hook.ProxyMethod;\nimport top.niunaijun.blackbox.utils.Slog;\nimport top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;",
        "import secure settings special access translator",
    )
    needle = '''            try {
                String spoofedAndroidId = getSpoofedAndroidId(args);'''
    replacement = '''            try {
                String specialAccessValue = LegacySpecialAccessCompat.translatedSecureString(args);
                if (specialAccessValue != null) {
                    return specialAccessValue;
                }
                String spoofedAndroidId = getSpoofedAndroidId(args);'''
    count = text.count(needle)
    if count != 2:
        raise SystemExit(f"[special-access] expected two string Settings hooks, found {count}")
    text = text.replace(needle, replacement, 2)
    print("[special-access] synthesize secure notification-listener state: applied")
    path.write_text(text, encoding="utf-8")


def patch_notification_manager(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/INotificationManagerProxy.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import android.content.Context;\nimport android.os.Build;",
        "import android.content.ComponentName;\nimport android.content.Context;\nimport android.os.Build;",
        "import ComponentName for notification access",
    )
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.utils.compat.BuildCompat;\nimport top.niunaijun.blackbox.utils.compat.ParceledListSliceCompat;",
        "import top.niunaijun.blackbox.utils.compat.BuildCompat;\nimport top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;\nimport top.niunaijun.blackbox.utils.compat.ParceledListSliceCompat;",
        "import notification special access translator",
    )
    insertion = r'''

    @ProxyMethod("isNotificationListenerAccessGranted")
    public static class IsNotificationListenerAccessGranted extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            ComponentName component = MethodParameterUtils.getFirstParam(args, ComponentName.class);
            if (LegacySpecialAccessCompat.isGuestComponent(component)) {
                return LegacySpecialAccessCompat.isNotificationListenerAccessGrantedForGuest(
                        component.getPackageName());
            }
            return method.invoke(who, args);
        }
    }

    @ProxyMethod("isNotificationPolicyAccessGranted")
    public static class IsNotificationPolicyAccessGranted extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            return LegacySpecialAccessCompat.isNotificationPolicyAccessGranted();
        }
    }

    @ProxyMethod("isNotificationPolicyAccessGrantedForPackage")
    public static class IsNotificationPolicyAccessGrantedForPackage extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            return LegacySpecialAccessCompat.isNotificationPolicyAccessGranted();
        }
    }

    @ProxyMethod("requestBindListener")
    public static class RequestBindListener extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            ComponentName component = MethodParameterUtils.getFirstParam(args, ComponentName.class);
            if (LegacySpecialAccessCompat.isGuestComponent(component)) {
                return null;
            }
            return method.invoke(who, args);
        }
    }

    @ProxyMethod("requestUnbindListener")
    public static class RequestUnbindListener extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            ComponentName component = MethodParameterUtils.getFirstParam(args, ComponentName.class);
            if (LegacySpecialAccessCompat.isGuestComponent(component)) {
                return null;
            }
            return method.invoke(who, args);
        }
    }
'''
    if "class IsNotificationListenerAccessGranted" not in text:
        pos = text.rfind("\n}")
        if pos < 0:
            raise SystemExit("[special-access] notification manager closing brace not found")
        text = text[:pos] + insertion + text[pos:]
        print("[special-access] notification access Binder hooks: applied")
    path.write_text(text, encoding="utf-8")


def patch_appops(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IAppOpsManagerProxy.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.utils.MethodParameterUtils;\nimport top.niunaijun.blackbox.utils.Slog;",
        "import top.niunaijun.blackbox.utils.MethodParameterUtils;\nimport top.niunaijun.blackbox.utils.Slog;\nimport top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;",
        "import AppOps special access translator",
    )
    old = '''        String methodName = method.getName();
        
        
        
        if (methodName.startsWith("check") || '''
    new = '''        String methodName = method.getName();

        // Special app access is enforced by real Android AppOps and must never be
        // fabricated as MODE_ALLOWED. Rewrite the virtual identity to the helper
        // package/UID, then ask the real service for the user's actual decision.
        if (LegacySpecialAccessCompat.isSpecialAccessAppOp(args)) {
            MethodParameterUtils.replaceAllAppPkg(args);
            MethodParameterUtils.replaceFirstUid(args);
            MethodParameterUtils.replaceLastUid(args);
            return method.invoke(getWho(), args);
        }
        
        
        
        if (methodName.startsWith("check") || '''
    text = replace_once(text, old, new, "make special AppOps truthful")
    path.write_text(text, encoding="utf-8")


def patch_service_dispatcher(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/app/dispatcher/AppServiceDispatcher.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import android.os.IBinder;",
        "import android.os.IBinder;\nimport android.service.notification.NotificationListenerService;\nimport android.service.notification.StatusBarNotification;",
        "import notification listener callback types",
    )
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.proxy.record.ProxyServiceRecord;",
        "import top.niunaijun.blackbox.proxy.record.ProxyServiceRecord;\nimport top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;",
        "import special access service bridge",
    )
    old = '''        try {
            int i = service.onStartCommand(stubRecord.mServiceIntent, flags, stubRecord.mStartId);
            BlackBoxCore.getBActivityManager().onStartCommand(proxyIntent, stubRecord.mUserId);
            return i;'''
    new = '''        try {
            int bridgeResult = dispatchSpecialAccessCallback(service, stubRecord.mServiceIntent);
            int i = bridgeResult != Integer.MIN_VALUE
                    ? bridgeResult
                    : service.onStartCommand(stubRecord.mServiceIntent, flags, stubRecord.mStartId);
            BlackBoxCore.getBActivityManager().onStartCommand(proxyIntent, stubRecord.mUserId);
            return i;'''
    text = replace_once(text, old, new, "dispatch real notification callbacks into guest service")
    method = r'''

    /**
     * A virtual NotificationListenerService cannot be bound by Android directly.
     * The real helper listener forwards callbacks as explicit virtual service starts;
     * translate those bridge starts back into the callbacks legacy code expects.
     */
    private int dispatchSpecialAccessCallback(Service service, Intent intent) {
        if (!(service instanceof NotificationListenerService) || intent == null) {
            return Integer.MIN_VALUE;
        }
        String action = intent.getAction();
        if (action == null || !action.startsWith("com.appcompat.engine.special.")) {
            return Integer.MIN_VALUE;
        }
        NotificationListenerService listener = (NotificationListenerService) service;
        try {
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_LISTENER_CONNECTED.equals(action)) {
                listener.onListenerConnected();
                return START_NOT_STICKY;
            }
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_LISTENER_DISCONNECTED.equals(action)) {
                listener.onListenerDisconnected();
                return START_NOT_STICKY;
            }
            StatusBarNotification sbn = intent.getParcelableExtra(
                    LegacySpecialAccessCompat.EXTRA_STATUS_BAR_NOTIFICATION);
            if (sbn == null) {
                return START_NOT_STICKY;
            }
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_POSTED.equals(action)) {
                listener.onNotificationPosted(sbn);
                return START_NOT_STICKY;
            }
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_REMOVED.equals(action)) {
                listener.onNotificationRemoved(sbn);
                return START_NOT_STICKY;
            }
        } catch (Throwable callbackFailure) {
            callbackFailure.printStackTrace();
            return START_NOT_STICKY;
        }
        return Integer.MIN_VALUE;
    }
'''
    if "dispatchSpecialAccessCallback(Service service" not in text:
        pos = text.rfind("\n}")
        if pos < 0:
            raise SystemExit("[special-access] service dispatcher closing brace not found")
        text = text[:pos] + method + text[pos:]
        print("[special-access] virtual notification listener callback dispatcher: applied")
    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    compat = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacySpecialAccessCompat.java"
    am = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/ActivityManagerCommonProxy.java").read_text(encoding="utf-8")
    settings = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/ISettingsProviderProxy.java").read_text(encoding="utf-8")
    notification = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/INotificationManagerProxy.java").read_text(encoding="utf-8")
    appops = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IAppOpsManagerProxy.java").read_text(encoding="utf-8")
    services = (root / "Bcore/src/main/java/top/niunaijun/blackbox/app/dispatcher/AppServiceDispatcher.java").read_text(encoding="utf-8")
    if not compat.is_file():
        raise SystemExit("[special-access] LegacySpecialAccessCompat missing")
    checks = [
        (am, "rewriteSettingsIntent"),
        (settings, "translatedSecureString"),
        (notification, "IsNotificationListenerAccessGranted"),
        (appops, "isSpecialAccessAppOp"),
        (services, "dispatchSpecialAccessCallback"),
    ]
    for content, invariant in checks:
        if invariant not in content:
            raise SystemExit(f"[special-access] invariant missing: {invariant}")
    print("[special-access] special app access translation verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_special_access_translation.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    write_compat(root)
    patch_start_activity(root)
    patch_settings_provider(root)
    patch_notification_manager(root)
    patch_appops(root)
    patch_service_dispatcher(root)
    verify(root)


if __name__ == "__main__":
    main()
