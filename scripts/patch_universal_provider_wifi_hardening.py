#!/usr/bin/env python3
"""Compile-safe and reflection-safe hardening for LegacyWifiCompat.

The public Android 36 SDK exposes configured Local Only Hotspot but its public
SoftApConfiguration.Builder SSID/passphrase setters only arrive in a later SDK revision.
Older modern Android releases have equivalent hidden builder/private WifiManager paths.
Use runtime reflection for configured LOHS where available and fall back to the stable
public startLocalOnlyHotspot API. Also expose one engine entry point used by the guest
LegacyReflectionCompat shim for APIs such as the removed setWifiApEnabled().
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[provider-wifi-hardening] {label}: already applied")
            return text
        raise SystemExit(f"[provider-wifi-hardening] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(
            f"[provider-wifi-hardening] {label}: expected one match, found {count}")
    print(f"[provider-wifi-hardening] {label}: applied")
    return text.replace(old, new, 1)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: patch_universal_provider_wifi_hardening.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    path = root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyWifiCompat.java"
    )
    text = path.read_text(encoding="utf-8")

    old_start = '''            try {
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
            } catch (Throwable failure) {'''
    new_start = '''            try {
                // Prefer a configured Local Only Hotspot so old apps that generated an
                // SSID/password can keep showing those same credentials. The relevant
                // builder/manager methods moved between hidden and public API surfaces
                // across Android releases, so resolve them at runtime instead of tying
                // this compatibility layer to one compile-SDK signature.
                boolean configured = requested != null
                        && requested.SSID != null
                        && requested.preSharedKey != null
                        && requested.preSharedKey.length() >= 8
                        && tryStartConfigured(manager, requested, callback);
                if (!configured) {
                    manager.startLocalOnlyHotspot(callback, callbackHandler());
                }
            } catch (Throwable failure) {'''
    text = replace_once(
        text, old_start, new_start,
        "replace API-36-only builder calls with runtime configured-LOHS bridge")

    marker = '''    private static boolean stop() {'''
    helpers = r'''    /**
     * Try the configured LOHS capability using whatever framework surface exists on
     * this Android release. Android 11-15 expose the necessary SoftAp builder and
     * WifiManager internal path as hidden APIs; Android 16 exposes the manager entry
     * point publicly. BlackBox already exempts its guest process from hidden-API
     * enforcement for legacy framework compatibility. Any failure simply falls back
     * to the normal public LOHS API rather than fabricating success.
     */
    private static boolean tryStartConfigured(
            WifiManager manager,
            WifiConfiguration requested,
            WifiManager.LocalOnlyHotspotCallback callback) {
        if (Build.VERSION.SDK_INT < 30 || requested == null) return false;
        try {
            SoftApConfiguration config = buildSoftApConfiguration(requested);
            if (config == null) return false;
            java.util.concurrent.Executor executor =
                    command -> callbackHandler().post(command);

            // Android 16 public surface.
            try {
                Method publicConfigured = WifiManager.class.getDeclaredMethod(
                        "startLocalOnlyHotspotWithConfiguration",
                        SoftApConfiguration.class,
                        java.util.concurrent.Executor.class,
                        WifiManager.LocalOnlyHotspotCallback.class);
                publicConfigured.setAccessible(true);
                publicConfigured.invoke(manager, config, executor, callback);
                return true;
            } catch (NoSuchMethodException ignored) {
            }

            // Android 11-15 implementation path. Match by shape to survive minor
            // signature/access changes without assuming a single vendor framework.
            for (Method method : WifiManager.class.getDeclaredMethods()) {
                if (!"startLocalOnlyHotspotInternal".equals(method.getName())) continue;
                Class<?>[] types = method.getParameterTypes();
                if (types.length != 4) continue;
                if (!SoftApConfiguration.class.isAssignableFrom(types[0])) continue;
                if (!java.util.concurrent.Executor.class.isAssignableFrom(types[1])) continue;
                if (!WifiManager.LocalOnlyHotspotCallback.class.isAssignableFrom(types[2])) {
                    continue;
                }
                if (!(types[3] == Boolean.TYPE || types[3] == Boolean.class)) continue;
                method.setAccessible(true);
                method.invoke(manager, config, executor, callback, false);
                return true;
            }
        } catch (Throwable failure) {
            Slog.w(TAG, "Configured local-only hotspot unavailable: " + failure);
        }
        return false;
    }

    private static SoftApConfiguration buildSoftApConfiguration(
            WifiConfiguration requested) {
        try {
            Class<?> builderClass = Class.forName(
                    "android.net.wifi.SoftApConfiguration$Builder");
            Object builder = builderClass.getDeclaredConstructor().newInstance();

            // setSsid(String) was hidden/SystemApi for years and later evolved into
            // setWifiSsid(WifiSsid). Prefer the historical form because it exactly
            // matches WifiConfiguration.SSID and exists on Android 11-16 frameworks.
            Method setSsid = findMethod(builderClass, "setSsid", String.class);
            if (setSsid != null) {
                setSsid.setAccessible(true);
                setSsid.invoke(builder, requested.SSID);
            } else {
                // Newer framework fallback: WifiSsid.fromUtf8Text + setWifiSsid.
                Class<?> wifiSsidClass = Class.forName("android.net.wifi.WifiSsid");
                Method fromUtf8 = findMethod(wifiSsidClass, "fromUtf8Text", String.class);
                Method setWifiSsid = findMethod(builderClass, "setWifiSsid", wifiSsidClass);
                if (fromUtf8 == null || setWifiSsid == null) return null;
                fromUtf8.setAccessible(true);
                setWifiSsid.setAccessible(true);
                Object wifiSsid = fromUtf8.invoke(null, requested.SSID);
                setWifiSsid.invoke(builder, wifiSsid);
            }

            Method setPassphrase = findMethod(
                    builderClass, "setPassphrase", String.class, Integer.TYPE);
            if (setPassphrase == null) return null;
            setPassphrase.setAccessible(true);
            setPassphrase.invoke(
                    builder,
                    requested.preSharedKey,
                    SoftApConfiguration.SECURITY_TYPE_WPA2_PSK);

            Method build = findMethod(builderClass, "build");
            if (build == null) return null;
            build.setAccessible(true);
            return (SoftApConfiguration) build.invoke(builder);
        } catch (Throwable failure) {
            Slog.w(TAG, "Could not construct configured SoftApConfiguration: " + failure);
            return null;
        }
    }

    private static Method findMethod(Class<?> owner, String name, Class<?>... args) {
        try {
            return owner.getMethod(name, args);
        } catch (Throwable ignored) {
        }
        try {
            return owner.getDeclaredMethod(name, args);
        } catch (Throwable ignored) {
            return null;
        }
    }

    /**
     * Entry point used by the guest reflection shim when a removed WifiManager method
     * no longer exists for Class.getMethod() to return on the current framework.
     */
    public static Object invokeLegacyReflection(
            String name, Object receiver, Object[] args) throws Exception {
        if (!(receiver instanceof WifiManager)) {
            throw new IllegalArgumentException("Legacy Wi-Fi receiver is not WifiManager");
        }
        WifiManager manager = (WifiManager) receiver;
        Object[] values = args != null ? args : new Object[0];

        if ("setWifiApEnabled".equals(name)) {
            WifiConfiguration config = values.length > 0
                    && values[0] instanceof WifiConfiguration
                    ? (WifiConfiguration) values[0] : null;
            boolean enabled = values.length > 1 && values[1] instanceof Boolean
                    ? (Boolean) values[1] : true;
            if (enabled) return start(config);
            return stop();
        }
        if ("isWifiApEnabled".equals(name)) {
            return sReservation != null;
        }
        if ("getWifiApState".equals(name)) {
            return sReservation != null ? WIFI_AP_STATE_ENABLED : WIFI_AP_STATE_DISABLED;
        }
        if ("getWifiApConfiguration".equals(name)) {
            return legacyConfiguration();
        }
        if ("setWifiApConfiguration".equals(name)) {
            if (values.length > 0 && values[0] instanceof WifiConfiguration) {
                sLegacyConfig = new WifiConfiguration((WifiConfiguration) values[0]);
                return true;
            }
            return false;
        }
        throw new NoSuchMethodException(name);
    }

'''
    if "public static Object invokeLegacyReflection" not in text:
        if marker not in text:
            raise SystemExit("[provider-wifi-hardening] helper insertion point missing")
        text = text.replace(marker, helpers + marker, 1)
        print("[provider-wifi-hardening] legacy reflection Wi-Fi bridge: applied")

    required = (
        "tryStartConfigured(manager, requested, callback)",
        '"startLocalOnlyHotspotInternal".equals(method.getName())',
        "public static Object invokeLegacyReflection(",
        '"setWifiApEnabled".equals(name)',
        'findMethod(builderClass, "setSsid", String.class)',
        "manager.startLocalOnlyHotspot(callback, callbackHandler())",
    )
    for item in required:
        if item not in text:
            raise SystemExit(f"[provider-wifi-hardening] verification failed: {item}")
    if ".setSsid(requested.SSID)" in text:
        raise SystemExit(
            "[provider-wifi-hardening] compile-SDK-specific direct setSsid call remains")

    path.write_text(text, encoding="utf-8")
    print("[provider-wifi-hardening] configured/fallback hotspot translation verified")


if __name__ == "__main__":
    main()
