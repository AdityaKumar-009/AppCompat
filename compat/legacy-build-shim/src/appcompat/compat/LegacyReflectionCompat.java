package appcompat.compat;

import android.net.wifi.WifiConfiguration;
import android.net.wifi.WifiManager;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;

/**
 * Transparent reflection bridge injected into legacy guest APKs.
 *
 * Old applications often discover hidden Android APIs through Class.getMethod() rather
 * than referencing them directly. When an API disappears from the framework class,
 * Binder hooks never get a chance to translate it. AppCompat structurally rewrites
 * Class.getMethod/getDeclaredMethod and Method.invoke calls to this class. Normal
 * reflection is delegated unchanged; only explicitly supported removed APIs receive a
 * synthetic Method sentinel and are translated by the engine at invocation time.
 */
public final class LegacyReflectionCompat {
    private static final String WIFI_MANAGER = "android.net.wifi.WifiManager";
    private static final String ENGINE_WIFI_BRIDGE =
            "top.niunaijun.blackbox.utils.compat.LegacyWifiCompat";

    private LegacyReflectionCompat() {}

    public static Method getMethod(Class<?> owner, String name, Class<?>[] parameterTypes)
            throws NoSuchMethodException, SecurityException {
        Method legacy = legacyMethod(owner, name, parameterTypes);
        return legacy != null ? legacy : owner.getMethod(name, parameterTypes);
    }

    public static Method getDeclaredMethod(Class<?> owner, String name, Class<?>[] parameterTypes)
            throws NoSuchMethodException, SecurityException {
        Method legacy = legacyMethod(owner, name, parameterTypes);
        return legacy != null ? legacy : owner.getDeclaredMethod(name, parameterTypes);
    }

    public static Object invoke(Method method, Object receiver, Object[] args)
            throws IllegalAccessException, InvocationTargetException {
        if (method != null && method.getDeclaringClass() == LegacyReflectionCompat.class
                && isLegacyWifiName(method.getName())) {
            try {
                Class<?> bridge = Class.forName(ENGINE_WIFI_BRIDGE);
                Method invoke = bridge.getMethod(
                        "invokeLegacyReflection", String.class, Object.class, Object[].class);
                return invoke.invoke(null, method.getName(), receiver,
                        args != null ? args : new Object[0]);
            } catch (InvocationTargetException failure) {
                // Preserve Method.invoke's contract: target exceptions are wrapped.
                throw failure;
            } catch (Throwable failure) {
                throw new InvocationTargetException(failure);
            }
        }
        return method.invoke(receiver, args);
    }

    private static Method legacyMethod(Class<?> owner, String name, Class<?>[] parameterTypes) {
        if (owner == null || !WIFI_MANAGER.equals(owner.getName()) || !isLegacyWifiName(name)) {
            return null;
        }
        Class<?>[] types = parameterTypes != null ? parameterTypes : new Class<?>[0];
        try {
            // These methods are sentinels only. Their signatures intentionally mirror
            // historical WifiManager reflection contracts so apps inspecting the Method
            // still see the parameter/return shapes they expect.
            return LegacyReflectionCompat.class.getDeclaredMethod(name, types);
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static boolean isLegacyWifiName(String name) {
        return "setWifiApEnabled".equals(name)
                || "isWifiApEnabled".equals(name)
                || "getWifiApState".equals(name)
                || "getWifiApConfiguration".equals(name)
                || "setWifiApConfiguration".equals(name);
    }

    // Reflection sentinels. Calls are intercepted by invoke() above.
    public static boolean setWifiApEnabled(WifiConfiguration config, boolean enabled) {
        return false;
    }

    public static boolean isWifiApEnabled() {
        return false;
    }

    public static int getWifiApState() {
        return 11;
    }

    public static WifiConfiguration getWifiApConfiguration() {
        return null;
    }

    public static boolean setWifiApConfiguration(WifiConfiguration config) {
        return false;
    }
}
