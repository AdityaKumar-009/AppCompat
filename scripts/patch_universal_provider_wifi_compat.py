#!/usr/bin/env python3
"""Universal compatibility for real content providers and legacy Wi-Fi hotspot APIs.

This pass is deliberately app-agnostic and addresses two broad Android migrations:

1. Virtual guest identity crossing into real system content providers.
   The real Binder caller is the AppCompat helper UID/package. Modern Android validates
   AttributionSource UID/package pairs, so forwarding the virtual guest package with the
   real helper UID can fail even when the helper genuinely holds the requested permission.
   Rewrite system-bound attribution to the helper identity. If a read query is still
   denied/fails, return an empty Cursor rather than null: this preserves privacy and
   prevents old pre-runtime-permission apps from crashing merely because they assume a
   manifest-declared provider query always returns a Cursor.

2. Pre-Oreo hidden Wi-Fi AP/tethering APIs.
   Many old transfer/automation apps reflectively call WifiManager.setWifiApEnabled(),
   getWifiApConfiguration() and isWifiApEnabled(). When those framework methods still
   reach IWifiManager on modern Android, translate them to the public Local Only Hotspot
   API instead of forwarding a privileged tethering operation that ordinary apps can no
   longer perform. Android remains the permission/policy authority.

No guest package names are special-cased.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[provider-wifi] {label}: already applied")
            return text
        raise SystemExit(f"[provider-wifi] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[provider-wifi] {label}: expected one match, found {count}")
    print(f"[provider-wifi] {label}: applied")
    return text.replace(old, new, 1)


def patch_attribution(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/AttributionSourceUtils.java"
    text = path.read_text(encoding="utf-8")

    old = '''    private static String getAttributionPackageName() {
        try {
            String appPackageName = BActivityThread.getAppPackageName();
            if (appPackageName != null && !appPackageName.isEmpty()) {
                return appPackageName;
            }
        } catch (Throwable ignored) {
        }
        return BlackBoxCore.getHostPkg();
    }'''
    new = '''    private static String getAttributionPackageName() {
        // AttributionSource is consumed by real system_server/system providers. The
        // Binder caller there is the helper UID, not the virtual guest UID. Android
        // validates the package name against that real UID, so exposing a guest package
        // here creates a mismatched identity and causes SecurityException on Contacts,
        // Calendar, MediaStore, Settings and many vendor providers.
        return BlackBoxCore.getHostPkg();
    }'''
    text = replace_once(text, old, new, "bind AttributionSource package to real helper UID")

    # Recursively normalize chained AttributionSource objects where available. Android
    # 12+ services may validate the full chain rather than only the first source.
    marker = '''            String[] packageFieldNames = {"mPackageName", "packageName", "mSourcePackage", "sourcePackage"};
            
            for (String fieldName : packageFieldNames) {
                try {
                    java.lang.reflect.Field packageField = attributionSourceClass.getDeclaredField(fieldName);
                    packageField.setAccessible(true);
                    packageField.set(attributionSource, getAttributionPackageName());
                    Slog.d(TAG, "Fixed AttributionSource package name via field: " + fieldName);
                    break;
                } catch (NoSuchFieldException e) {
                    
                }
            }
            
        } catch (Exception e) {'''
    replacement = '''            String[] packageFieldNames = {"mPackageName", "packageName", "mSourcePackage", "sourcePackage"};
            
            for (String fieldName : packageFieldNames) {
                try {
                    java.lang.reflect.Field packageField = attributionSourceClass.getDeclaredField(fieldName);
                    packageField.setAccessible(true);
                    packageField.set(attributionSource, getAttributionPackageName());
                    Slog.d(TAG, "Fixed AttributionSource package name via field: " + fieldName);
                    break;
                } catch (NoSuchFieldException e) {
                    
                }
            }

            // Tags are scoped to a real package. A tag copied from a virtual package
            // may itself fail attribution validation, so clear it at the system edge.
            for (String fieldName : new String[]{"mAttributionTag", "attributionTag"}) {
                try {
                    java.lang.reflect.Field tagField = attributionSourceClass.getDeclaredField(fieldName);
                    tagField.setAccessible(true);
                    tagField.set(attributionSource, null);
                    break;
                } catch (Throwable ignored) {
                }
            }

            // Normalize a chained source too when the platform stores one directly.
            for (String fieldName : new String[]{"mNext", "next"}) {
                try {
                    java.lang.reflect.Field nextField = attributionSourceClass.getDeclaredField(fieldName);
                    nextField.setAccessible(true);
                    Object next = nextField.get(attributionSource);
                    if (next != null && next != attributionSource) {
                        fixAttributionSourceUid(next);
                    }
                    break;
                } catch (Throwable ignored) {
                }
            }
            
        } catch (Exception e) {'''
    text = replace_once(text, marker, replacement, "normalize chained attribution identity")
    path.write_text(text, encoding="utf-8")


def patch_content_provider(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IContentProviderProxy.java"
    text = path.read_text(encoding="utf-8")

    if "import android.database.MatrixCursor;" not in text:
        text = text.replace(
            "package top.niunaijun.blackbox.fake.service;\n\n",
            "package top.niunaijun.blackbox.fake.service;\n\n"
            "import android.database.MatrixCursor;\n\n",
            1,
        )

    old = '''    @ProxyMethod("query")
    public static class Query extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            try {
                
                AttributionSourceUtils.fixAttributionSourceInArgs(args);
                
                
                return method.invoke(who, args);
            } catch (Exception e) {
                Slog.w(TAG, "Error in query hook: " + e.getMessage());
                
                return null;
            }
        }
    }'''
    new = '''    @ProxyMethod("query")
    public static class Query extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            try {
                AttributionSourceUtils.fixAttributionSourceInArgs(args);
                return method.invoke(who, args);
            } catch (Throwable failure) {
                Slog.w(TAG, "Provider query failed after attribution translation: "
                        + failure.getMessage());

                // Returning null is dangerous for old apps written before runtime
                // permissions: many immediately call moveToFirst()/getCount(). Modern
                // ContentProvider itself uses an empty cursor for denied reads. Mirror
                // that privacy-preserving behavior where the reflected return contract
                // is Cursor-compatible.
                if (android.database.Cursor.class.isAssignableFrom(method.getReturnType())) {
                    return new MatrixCursor(findProjection(args));
                }
                return null;
            }
        }

        private static String[] findProjection(Object[] args) {
            if (args != null) {
                for (Object arg : args) {
                    if (arg instanceof String[]) {
                        String[] values = (String[]) arg;
                        // The first String[] in IContentProvider.query is the projection.
                        return values != null ? values : new String[0];
                    }
                }
            }
            return new String[0];
        }
    }'''
    text = replace_once(text, old, new, "return privacy-safe empty cursor on provider query failure")
    path.write_text(text, encoding="utf-8")


def patch_permission_semantics(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyPermissionCompat.java"
    text = path.read_text(encoding="utf-8")

    marker = '''        Set<String> hostDeclared = hostDeclaredPermissions(context);
        LinkedHashSet<String> out = new LinkedHashSet<>();
        for (String guestPermission : guest.requestedPermissions) {'''
    replacement = '''        Set<String> hostDeclared = hostDeclaredPermissions(context);
        LinkedHashSet<String> out = new LinkedHashSet<>();

        // Legacy hotspot creation used only CHANGE_WIFI_STATE. Its public modern
        // equivalent (Local Only Hotspot) requires fine location for a pre-T target.
        // Translate that semantic requirement during helper preflight so old transfer,
        // automation and device-control apps can legitimately use the modern API.
        if (Build.VERSION.SDK_INT >= 26
                && Arrays.asList(guest.requestedPermissions)
                        .contains(Manifest.permission.CHANGE_WIFI_STATE)
                && hostDeclared.contains(Manifest.permission.ACCESS_FINE_LOCATION)
                && context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                        != PackageManager.PERMISSION_GRANTED) {
            out.add(Manifest.permission.ACCESS_FINE_LOCATION);
        }

        for (String guestPermission : guest.requestedPermissions) {'''
    text = replace_once(text, marker, replacement, "map legacy hotspot permission to modern location grant")

    # READ_PROFILE disappeared as an independently useful modern runtime grant. Profile
    # reads are covered by Contacts access; map old declarations accordingly.
    marker2 = '''        if (Manifest.permission.ACCESS_FINE_LOCATION.equals(permission)) {'''
    replacement2 = '''        if ("android.permission.READ_PROFILE".equals(permission)) {
            return Collections.singletonList(Manifest.permission.READ_CONTACTS);
        }

        if (Manifest.permission.ACCESS_FINE_LOCATION.equals(permission)) {'''
    text = replace_once(text, marker2, replacement2, "map READ_PROFILE to modern READ_CONTACTS")
    path.write_text(text, encoding="utf-8")


def write_wifi_compat(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyWifiCompat.java"
    source = r'''package top.niunaijun.blackbox.utils.compat;

import android.content.Context;
import android.net.wifi.SoftApConfiguration;
import android.net.wifi.WifiConfiguration;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;

import java.lang.reflect.Method;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import top.niunaijun.blackbox.BlackBoxCore;
import top.niunaijun.blackbox.utils.Slog;

/** Modern backing for pre-Oreo WifiManager AP/tethering calls where possible. */
public final class LegacyWifiCompat {
    private static final String TAG = "LegacyWifiCompat";
    private static final int WIFI_AP_STATE_DISABLED = 11;
    private static final int WIFI_AP_STATE_ENABLED = 13;
    private static final Object LOCK = new Object();

    private static volatile WifiManager.LocalOnlyHotspotReservation sReservation;
    private static volatile WifiConfiguration sLegacyConfig;
    private static HandlerThread sCallbackThread;

    private LegacyWifiCompat() {}

    /** Returns a special handled value, or NOT_HANDLED when normal Binder dispatch wins. */
    public static final Object NOT_HANDLED = new Object();

    public static Object maybeHandle(Method method, Object[] args) {
        if (method == null || Build.VERSION.SDK_INT < 26) return NOT_HANDLED;
        String name = method.getName();

        if ("setWifiApEnabled".equals(name) || "startSoftAp".equals(name)) {
            WifiConfiguration requested = firstWifiConfiguration(args);
            boolean enabled = lastBoolean(args, true);
            boolean ok = enabled ? start(requested) : stop();
            return adaptBooleanReturn(method, ok);
        }
        if ("stopSoftAp".equals(name)) {
            boolean ok = stop();
            return adaptBooleanReturn(method, ok);
        }
        if ("getWifiApEnabledState".equals(name) || "getWifiApState".equals(name)) {
            return sReservation != null ? WIFI_AP_STATE_ENABLED : WIFI_AP_STATE_DISABLED;
        }
        if ("isWifiApEnabled".equals(name)) {
            return sReservation != null;
        }
        if ("getWifiApConfiguration".equals(name)) {
            return legacyConfiguration();
        }
        return NOT_HANDLED;
    }

    private static boolean start(WifiConfiguration requested) {
        synchronized (LOCK) {
            if (sReservation != null) return true;

            Context context = BlackBoxCore.getContext();
            if (context == null) return false;
            WifiManager manager = (WifiManager) context.getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (manager == null) return false;

            ensureCallbackThread();
            CountDownLatch latch = new CountDownLatch(1);
            final boolean[] success = new boolean[]{false};
            WifiManager.LocalOnlyHotspotCallback callback =
                    new WifiManager.LocalOnlyHotspotCallback() {
                @Override
                public void onStarted(WifiManager.LocalOnlyHotspotReservation reservation) {
                    synchronized (LOCK) {
                        sReservation = reservation;
                        sLegacyConfig = toLegacyConfiguration(reservation);
                        success[0] = true;
                    }
                    latch.countDown();
                }

                @Override
                public void onStopped() {
                    synchronized (LOCK) {
                        sReservation = null;
                        sLegacyConfig = null;
                    }
                    latch.countDown();
                }

                @Override
                public void onFailed(int reason) {
                    Slog.w(TAG, "Local-only hotspot failed, reason=" + reason);
                    latch.countDown();
                }
            };

            try {
                // Android 16 can preserve the legacy app's requested SSID/password.
                // Older releases choose secure random credentials; callers that query
                // getWifiApConfiguration() receive those real credentials.
                if (Build.VERSION.SDK_INT >= 36 && requested != null
                        && requested.SSID != null && requested.preSharedKey != null
                        && requested.preSharedKey.length() >= 8) {
                    SoftApConfiguration config = new SoftApConfiguration.Builder()
                            .setSsid(requested.SSID)
                            .setPassphrase(
                                    requested.preSharedKey,
                                    SoftApConfiguration.SECURITY_TYPE_WPA2_PSK)
                            .build();
                    manager.startLocalOnlyHotspotWithConfiguration(
                            config,
                            command -> callbackHandler().post(command),
                            callback);
                } else {
                    manager.startLocalOnlyHotspot(callback, callbackHandler());
                }
            } catch (Throwable failure) {
                Slog.w(TAG, "Could not start modern local-only hotspot: " + failure);
                return false;
            }

            try {
                latch.await(8, TimeUnit.SECONDS);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            }
            return success[0] || sReservation != null;
        }
    }

    private static boolean stop() {
        synchronized (LOCK) {
            WifiManager.LocalOnlyHotspotReservation reservation = sReservation;
            sReservation = null;
            sLegacyConfig = null;
            if (reservation != null) {
                try {
                    reservation.close();
                } catch (Throwable ignored) {
                }
            }
            return true;
        }
    }

    @SuppressWarnings("deprecation")
    private static WifiConfiguration toLegacyConfiguration(
            WifiManager.LocalOnlyHotspotReservation reservation) {
        if (reservation == null) return null;
        try {
            if (Build.VERSION.SDK_INT >= 30) {
                SoftApConfiguration soft = reservation.getSoftApConfiguration();
                WifiConfiguration legacy = new WifiConfiguration();
                legacy.SSID = soft.getSsid();
                legacy.preSharedKey = soft.getPassphrase();
                return legacy;
            }
            return reservation.getWifiConfiguration();
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static WifiConfiguration legacyConfiguration() {
        WifiConfiguration config = sLegacyConfig;
        return config != null ? config : new WifiConfiguration();
    }

    private static WifiConfiguration firstWifiConfiguration(Object[] args) {
        if (args != null) {
            for (Object arg : args) if (arg instanceof WifiConfiguration) return (WifiConfiguration) arg;
        }
        return null;
    }

    private static boolean lastBoolean(Object[] args, boolean fallback) {
        if (args != null) {
            for (int i = args.length - 1; i >= 0; i--) {
                if (args[i] instanceof Boolean) return (Boolean) args[i];
            }
        }
        return fallback;
    }

    private static Object adaptBooleanReturn(Method method, boolean value) {
        Class<?> type = method.getReturnType();
        if (type == Boolean.TYPE || type == Boolean.class) return value;
        if (type == Integer.TYPE || type == Integer.class) return value ? 1 : 0;
        return null;
    }

    private static void ensureCallbackThread() {
        if (sCallbackThread != null) return;
        synchronized (LOCK) {
            if (sCallbackThread == null) {
                sCallbackThread = new HandlerThread("AppCompatLegacyHotspot");
                sCallbackThread.start();
            }
        }
    }

    private static Handler callbackHandler() {
        ensureCallbackThread();
        return new Handler(sCallbackThread.getLooper());
    }
}
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    print("[provider-wifi] LegacyWifiCompat: written")


def patch_wifi_proxy(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IWifiManagerProxy.java"
    text = path.read_text(encoding="utf-8")

    imports = '''import top.niunaijun.blackbox.fake.hook.ProxyMethod;'''
    replacement = '''import top.niunaijun.blackbox.fake.hook.ProxyMethod;
import top.niunaijun.blackbox.utils.AttributionSourceUtils;
import top.niunaijun.blackbox.utils.MethodParameterUtils;
import top.niunaijun.blackbox.utils.compat.LegacyWifiCompat;'''
    text = replace_once(text, imports, replacement, "import generic Wi-Fi translators")

    marker = '''    @Override
    public boolean isBadEnv() {
        return false;
    }
'''
    insertion = '''    @Override
    public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
        // Every real WifiService call belongs to the helper UID/package. Rewrite
        // virtual package/UID/AttributionSource arguments before system_server sees
        // them, then translate legacy AP methods when a modern public equivalent exists.
        MethodParameterUtils.replaceAllAppPkg(args);
        MethodParameterUtils.replaceFirstUid(args);
        AttributionSourceUtils.fixAttributionSourceInArgs(args);

        Object translated = LegacyWifiCompat.maybeHandle(method, args);
        if (translated != LegacyWifiCompat.NOT_HANDLED) {
            return translated;
        }
        return super.invoke(proxy, method, args);
    }

''' + marker
    text = replace_once(text, marker, insertion, "translate WifiService identity + legacy hotspot calls")

    # getConnectionInfo may legitimately return null when Wi-Fi is unavailable; the
    # upstream hook dereferences it unconditionally. Keep the compatibility spoof only
    # when a real object exists.
    old = '''            WifiInfo wifiInfo = (WifiInfo) method.invoke(who, args);
            BRWifiInfo.get(wifiInfo)._set_mBSSID("ac:62:5a:82:65:c4");
            BRWifiInfo.get(wifiInfo)._set_mMacAddress("ac:62:5a:82:65:c4");
            BRWifiInfo.get(wifiInfo)._set_mWifiSsid(BRWifiSsid.get().createFromAsciiEncoded("BlackBox_Wifi"));
            return wifiInfo;'''
    new = '''            WifiInfo wifiInfo = (WifiInfo) method.invoke(who, args);
            if (wifiInfo != null) {
                BRWifiInfo.get(wifiInfo)._set_mBSSID("ac:62:5a:82:65:c4");
                BRWifiInfo.get(wifiInfo)._set_mMacAddress("ac:62:5a:82:65:c4");
                BRWifiInfo.get(wifiInfo)._set_mWifiSsid(
                        BRWifiSsid.get().createFromAsciiEncoded("BlackBox_Wifi"));
            }
            return wifiInfo;'''
    text = replace_once(text, old, new, "make getConnectionInfo null-safe")
    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    attrib = (root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/AttributionSourceUtils.java").read_text(encoding="utf-8")
    provider = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IContentProviderProxy.java").read_text(encoding="utf-8")
    perms = (root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyPermissionCompat.java").read_text(encoding="utf-8")
    wifi = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IWifiManagerProxy.java").read_text(encoding="utf-8")
    compat = (root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyWifiCompat.java").read_text(encoding="utf-8")

    checks = [
        (attrib, "return BlackBoxCore.getHostPkg();"),
        (attrib, 'new String[]{"mNext", "next"}'),
        (provider, "new MatrixCursor(findProjection(args))"),
        (perms, 'contains(Manifest.permission.CHANGE_WIFI_STATE)'),
        (perms, '"android.permission.READ_PROFILE".equals(permission)'),
        (wifi, "LegacyWifiCompat.maybeHandle(method, args)"),
        (wifi, "AttributionSourceUtils.fixAttributionSourceInArgs(args)"),
        (compat, "startLocalOnlyHotspot(callback, callbackHandler())"),
        (compat, "startLocalOnlyHotspotWithConfiguration"),
    ]
    for source, needle in checks:
        if needle not in source:
            raise SystemExit(f"[provider-wifi] verification failed: {needle}")

    if "return appPackageName;" in attrib:
        raise SystemExit("[provider-wifi] guest package still leaks into system AttributionSource")
    print("[provider-wifi] provider identity + empty-query + modern hotspot bridge verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_universal_provider_wifi_compat.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    patch_attribution(root)
    patch_content_provider(root)
    patch_permission_semantics(root)
    write_wifi_compat(root)
    patch_wifi_proxy(root)
    verify(root)


if __name__ == "__main__":
    main()
