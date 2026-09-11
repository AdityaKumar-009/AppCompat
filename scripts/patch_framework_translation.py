#!/usr/bin/env python3
"""Add runtime Android framework type/signature translation to the pinned engine.

The compatibility engine intentionally runs old guest applications against the
framework interfaces exposed by the device. Those hidden Binder interfaces evolve
between Android releases. The upstream engine dispatches hooks by method name only,
so a legacy fixed-value hook can accidentally return (for example) int[] when the
runtime interface expects ParceledListSlice. ART's dynamic proxy then aborts the
guest before application code can recover.

This patch makes the hook boundary self-describing: every hooked result is checked
against Method.getReturnType() from the *actual device framework*. Known container
shape changes are translated, incompatible legacy hooks fall back to the real
service when possible, and the final fallback is always type-safe. It also repairs
the upstream SDK gates for Android 13/14 and adds Android 12L/15/16 helpers.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[framework-translation] {label}: already applied")
            return text
        raise SystemExit(f"[framework-translation] {label}: expected source pattern not found")
    if count != 1:
        raise SystemExit(
            f"[framework-translation] {label}: expected exactly one match, found {count}"
        )
    print(f"[framework-translation] {label}: applied")
    return text.replace(old, new, 1)


def patch_build_compat(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/BuildCompat.java"
    text = path.read_text(encoding="utf-8")

    if "public static boolean isBaklava()" not in text:
        start_marker = "    public static boolean isU() {"
        end_marker = (
            "    public static boolean isL() {\n"
            "        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP;\n"
            "    }"
        )
        start = text.find(start_marker)
        end_start = text.find(end_marker)
        if start < 0 or end_start < 0 or end_start < start:
            raise SystemExit("[framework-translation] BuildCompat API block not found")
        end = end_start + len(end_marker)
        replacement = '''    /**
     * True when the running framework is at least {@code api}. Preview builds
     * report the previous stable SDK_INT plus a non-zero PREVIEW_SDK_INT, so keep
     * that case explicit instead of scattering off-by-one checks across hooks.
     */
    public static boolean isAtLeast(int api) {
        if (Build.VERSION.SDK_INT >= api) {
            return true;
        }
        return Build.VERSION.SDK_INT == api - 1 && getPreviewSDKInt() > 0;
    }

    /** Android 16 / API 36. */
    public static boolean isBaklava() {
        return isAtLeast(36);
    }

    /** Android 15 / API 35. */
    public static boolean isV() {
        return isAtLeast(35);
    }

    /** Android 14 / API 34. */
    public static boolean isU() {
        return isAtLeast(34);
    }

    /** Android 13 / API 33. */
    public static boolean isTiramisu() {
        return isAtLeast(33);
    }

    /** Android 12L / API 32. */
    public static boolean isSv2() {
        return isAtLeast(32);
    }

    public static boolean isS() {
        return isAtLeast(31);
    }

    public static boolean isR() {
        return isAtLeast(30);
    }

    public static boolean isQ() {
        return isAtLeast(29);
    }

    public static boolean isPie() {
        return isAtLeast(28);
    }

    public static boolean isOreo() {
        return isAtLeast(26);
    }

    public static boolean isN_MR1() {
        return isAtLeast(25);
    }

    public static boolean isN() {
        return isAtLeast(24);
    }

    public static boolean isM() {
        return isAtLeast(23);
    }

    public static boolean isL() {
        return isAtLeast(21);
    }'''
        text = text[:start] + replacement + text[end:]
        print("[framework-translation] corrected Android SDK gates: applied")
    else:
        print("[framework-translation] corrected Android SDK gates: already applied")

    path.write_text(text, encoding="utf-8")


def write_type_adapter(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/FrameworkTypeAdapter.java"
    source = r'''package top.niunaijun.blackbox.utils.compat;

import android.app.Application;
import android.os.Build;
import android.os.Parcelable;
import android.util.Log;

import java.lang.reflect.Array;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

import top.niunaijun.blackbox.app.BActivityThread;

/**
 * Runtime adapter for hidden Android framework/Binder API drift.
 *
 * BlackBox hooks are registered by method name while Android's hidden service
 * interfaces can change signatures and container return types between releases.
 * Never let a stale hook value cross a java.lang.reflect.Proxy boundary with the
 * wrong type: ART rejects it before the guest can handle the call.
 */
public final class FrameworkTypeAdapter {
    private static final String TAG = "FrameworkTypeAdapter";

    private FrameworkTypeAdapter() {
    }

    public static Object adaptReturnValue(
            Object base, Method method, Object[] args, Object value) {
        if (method == null) {
            return value;
        }

        Class<?> expected = method.getReturnType();
        if (expected == Void.TYPE) {
            return null;
        }
        if (isCompatible(expected, value)) {
            return value;
        }

        Object converted = convertLosslessly(method, expected, value);
        if (isCompatible(expected, converted)) {
            logTranslation(method, value, converted, "converted");
            return converted;
        }

        // A type mismatch means this name-only hook no longer describes the
        // runtime method. Prefer the device service's real implementation before
        // fabricating data. This preserves modern semantics whenever permissions
        // and the host identity allow the call.
        try {
            Object platformValue = method.invoke(base, args);
            if (isCompatible(expected, platformValue)) {
                logTranslation(method, value, platformValue, "platform-fallback");
                return platformValue;
            }

            Object platformConverted = convertLosslessly(method, expected, platformValue);
            if (isCompatible(expected, platformConverted)) {
                logTranslation(method, platformValue, platformConverted,
                        "platform-converted");
                return platformConverted;
            }
        } catch (Throwable platformFailure) {
            Throwable cause = unwrap(platformFailure);
            Log.w(TAG, "Platform fallback unavailable for " + method.getName()
                    + ": " + cause.getClass().getSimpleName() + ": " + cause.getMessage());
        }

        Object fallback = safeDefault(method, expected);
        logTranslation(method, value, fallback, "type-safe-default");
        return fallback;
    }

    /**
     * Recover only failures that are characteristic of framework signature drift.
     * Guest/application exceptions are deliberately not swallowed here.
     */
    public static boolean isVersionSkewFailure(Throwable failure) {
        Throwable cause = unwrap(failure);
        return cause instanceof ClassCastException
                || cause instanceof IllegalArgumentException
                || cause instanceof AbstractMethodError
                || cause instanceof NoSuchMethodError
                || cause instanceof IncompatibleClassChangeError;
    }

    public static Object recoverHookFailure(
            Object base, Method method, Object[] args, Throwable hookFailure) throws Throwable {
        if (!isVersionSkewFailure(hookFailure)) {
            throw hookFailure;
        }

        Throwable cause = unwrap(hookFailure);
        Log.w(TAG, "Recovering version-skewed hook " + method.toGenericString()
                + " on SDK " + Build.VERSION.SDK_INT
                + " guestTarget=" + guestTargetSdk()
                + " after " + cause.getClass().getSimpleName() + ": " + cause.getMessage());

        try {
            Object platformValue = method.invoke(base, args);
            return adaptReturnValue(null, method, null, platformValue);
        } catch (Throwable platformFailure) {
            Throwable platformCause = unwrap(platformFailure);
            Log.w(TAG, "Version-skew platform recovery failed for " + method.getName()
                    + ": " + platformCause.getClass().getSimpleName()
                    + ": " + platformCause.getMessage());
            return safeDefault(method, method.getReturnType());
        }
    }

    private static Object convertLosslessly(Method method, Class<?> expected, Object value) {
        if (value == null) {
            return expected.isPrimitive() ? primitiveDefault(expected) : null;
        }

        if (isNumeric(expected) && value instanceof Number) {
            return convertNumber(expected, (Number) value);
        }
        if ((expected == Boolean.TYPE || expected == Boolean.class) && value instanceof Boolean) {
            return value;
        }
        if ((expected == Character.TYPE || expected == Character.class)
                && value instanceof Character) {
            return value;
        }

        if (expected.isArray()) {
            return convertToArray(expected.getComponentType(), value);
        }

        if (Collection.class.isAssignableFrom(expected)) {
            List<?> list = unwrapListLike(value);
            if (list != null) {
                if (Set.class.isAssignableFrom(expected)) {
                    return new LinkedHashSet<Object>(list);
                }
                if (expected.isInterface() || expected.isAssignableFrom(ArrayList.class)) {
                    return new ArrayList<Object>(list);
                }
            }
        }

        if (ParceledListSliceCompat.isReturnParceledListSlice(method)) {
            List<?> list = asParcelableList(value);
            if (list != null) {
                try {
                    return ParceledListSliceCompat.create(list);
                } catch (Throwable ignored) {
                    // Hidden framework constructor can differ on vendor builds;
                    // platform fallback/default handling below remains type-safe.
                }
            }
        }

        return null;
    }

    private static Object convertToArray(Class<?> component, Object value) {
        List<?> values = toList(value);
        if (values == null) {
            return null;
        }

        Object out = Array.newInstance(component, values.size());
        try {
            for (int i = 0; i < values.size(); i++) {
                Object item = values.get(i);
                Object converted = convertArrayElement(component, item);
                if (!isCompatible(component, converted)) {
                    return null;
                }
                Array.set(out, i, converted);
            }
            return out;
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static Object convertArrayElement(Class<?> component, Object value) {
        if (isCompatible(component, value)) {
            return value;
        }
        if (isNumeric(component) && value instanceof Number) {
            return convertNumber(component, (Number) value);
        }
        if (value == null && component.isPrimitive()) {
            return primitiveDefault(component);
        }
        return null;
    }

    private static List<?> unwrapListLike(Object value) {
        if (value instanceof Collection<?>) {
            return new ArrayList<Object>((Collection<?>) value);
        }
        List<?> array = toList(value);
        if (array != null) {
            return array;
        }

        // Reverse bridge: some releases return ParceledListSlice where nearby
        // releases expose a List directly.
        if ("android.content.pm.ParceledListSlice".equals(value.getClass().getName())) {
            try {
                Method getList = value.getClass().getDeclaredMethod("getList");
                getList.setAccessible(true);
                Object list = getList.invoke(value);
                if (list instanceof List<?>) {
                    return (List<?>) list;
                }
            } catch (Throwable ignored) {
            }
        }
        return null;
    }

    private static List<?> asParcelableList(Object value) {
        List<?> list = unwrapListLike(value);
        if (list == null) {
            return null;
        }
        for (Object item : list) {
            if (item != null && !(item instanceof Parcelable)) {
                return null;
            }
        }
        return list;
    }

    private static List<?> toList(Object value) {
        if (value == null || !value.getClass().isArray()) {
            return null;
        }
        int length = Array.getLength(value);
        ArrayList<Object> list = new ArrayList<Object>(length);
        for (int i = 0; i < length; i++) {
            list.add(Array.get(value, i));
        }
        return list;
    }

    private static Object safeDefault(Method method, Class<?> expected) {
        if (expected == Void.TYPE) {
            return null;
        }
        if (expected.isPrimitive()) {
            return primitiveDefault(expected);
        }
        if (expected.isArray()) {
            return Array.newInstance(expected.getComponentType(), 0);
        }
        if (ParceledListSliceCompat.isReturnParceledListSlice(method)) {
            try {
                return ParceledListSliceCompat.create(Collections.emptyList());
            } catch (Throwable ignored) {
                return null;
            }
        }
        if (Set.class.isAssignableFrom(expected)) {
            return Collections.emptySet();
        }
        if (Collection.class.isAssignableFrom(expected)) {
            return Collections.emptyList();
        }
        return null;
    }

    private static boolean isCompatible(Class<?> expected, Object value) {
        if (value == null) {
            return !expected.isPrimitive();
        }
        if (!expected.isPrimitive()) {
            return expected.isInstance(value);
        }
        if (expected == Boolean.TYPE) return value instanceof Boolean;
        if (expected == Character.TYPE) return value instanceof Character;
        if (expected == Byte.TYPE) return value instanceof Byte;
        if (expected == Short.TYPE) return value instanceof Short;
        if (expected == Integer.TYPE) return value instanceof Integer;
        if (expected == Long.TYPE) return value instanceof Long;
        if (expected == Float.TYPE) return value instanceof Float;
        if (expected == Double.TYPE) return value instanceof Double;
        return false;
    }

    private static boolean isNumeric(Class<?> type) {
        return type == Byte.TYPE || type == Byte.class
                || type == Short.TYPE || type == Short.class
                || type == Integer.TYPE || type == Integer.class
                || type == Long.TYPE || type == Long.class
                || type == Float.TYPE || type == Float.class
                || type == Double.TYPE || type == Double.class;
    }

    private static Object convertNumber(Class<?> expected, Number value) {
        if (expected == Byte.TYPE || expected == Byte.class) return value.byteValue();
        if (expected == Short.TYPE || expected == Short.class) return value.shortValue();
        if (expected == Integer.TYPE || expected == Integer.class) return value.intValue();
        if (expected == Long.TYPE || expected == Long.class) return value.longValue();
        if (expected == Float.TYPE || expected == Float.class) return value.floatValue();
        if (expected == Double.TYPE || expected == Double.class) return value.doubleValue();
        return null;
    }

    private static Object primitiveDefault(Class<?> type) {
        if (type == Boolean.TYPE) return false;
        if (type == Character.TYPE) return '\0';
        if (type == Byte.TYPE) return (byte) 0;
        if (type == Short.TYPE) return (short) 0;
        if (type == Integer.TYPE) return 0;
        if (type == Long.TYPE) return 0L;
        if (type == Float.TYPE) return 0f;
        if (type == Double.TYPE) return 0d;
        return null;
    }

    private static Throwable unwrap(Throwable failure) {
        Throwable current = failure;
        while (current instanceof InvocationTargetException
                && ((InvocationTargetException) current).getCause() != null) {
            current = ((InvocationTargetException) current).getCause();
        }
        return current;
    }

    private static int guestTargetSdk() {
        try {
            Application application = BActivityThread.getApplication();
            if (application != null && application.getApplicationInfo() != null) {
                return application.getApplicationInfo().targetSdkVersion;
            }
        } catch (Throwable ignored) {
        }
        return -1;
    }

    private static void logTranslation(
            Method method, Object from, Object to, String strategy) {
        String fromType = from == null ? "null" : from.getClass().getName();
        String toType = to == null ? "null" : to.getClass().getName();
        Log.w(TAG, "Framework return translation [" + strategy + "] method="
                + method.toGenericString() + " from=" + fromType + " to=" + toType
                + " sdk=" + Build.VERSION.SDK_INT + " guestTarget=" + guestTargetSdk());
    }
}
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == source:
        print("[framework-translation] FrameworkTypeAdapter: already current")
    else:
        path.write_text(source, encoding="utf-8")
        print("[framework-translation] FrameworkTypeAdapter: written")


def patch_dispatcher(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/hook/ClassInvocationStub.java"
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "import top.niunaijun.blackbox.utils.MethodParameterUtils;",
        "import top.niunaijun.blackbox.utils.MethodParameterUtils;\n"
        "import top.niunaijun.blackbox.utils.compat.FrameworkTypeAdapter;",
        "import framework type adapter",
    )

    old = '''        Object result = methodHook.beforeHook(mBase, method, args);
        if (result != null) {
            return result;
        }
        result = methodHook.hook(mBase, method, args);
        result = methodHook.afterHook(result);
        return result;'''
    new = '''        try {
            Object result = methodHook.beforeHook(mBase, method, args);
            if (result != null) {
                return FrameworkTypeAdapter.adaptReturnValue(mBase, method, args, result);
            }
            result = methodHook.hook(mBase, method, args);
            result = methodHook.afterHook(result);
            return FrameworkTypeAdapter.adaptReturnValue(mBase, method, args, result);
        } catch (Throwable hookFailure) {
            if (FrameworkTypeAdapter.isVersionSkewFailure(hookFailure)) {
                return FrameworkTypeAdapter.recoverHookFailure(
                        mBase, method, args, hookFailure);
            }
            throw hookFailure;
        }'''
    text = replace_once(text, old, new, "make Binder hook results runtime-type-aware")

    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    build_compat = (root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/BuildCompat.java").read_text(encoding="utf-8")
    dispatcher = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/hook/ClassInvocationStub.java").read_text(encoding="utf-8")
    adapter = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/FrameworkTypeAdapter.java"

    required_build = [
        "public static boolean isAtLeast(int api)",
        "public static boolean isBaklava()",
        "return isAtLeast(36);",
        "public static boolean isV()",
        "return isAtLeast(35);",
        "public static boolean isU()",
        "return isAtLeast(34);",
        "public static boolean isTiramisu()",
        "return isAtLeast(33);",
        "public static boolean isSv2()",
        "return isAtLeast(32);",
    ]
    for invariant in required_build:
        if invariant not in build_compat:
            raise SystemExit(f"[framework-translation] BuildCompat verification failed: {invariant}")

    required_dispatch = [
        "FrameworkTypeAdapter.adaptReturnValue(mBase, method, args, result)",
        "FrameworkTypeAdapter.isVersionSkewFailure(hookFailure)",
        "FrameworkTypeAdapter.recoverHookFailure(",
    ]
    for invariant in required_dispatch:
        if invariant not in dispatcher:
            raise SystemExit(f"[framework-translation] dispatcher verification failed: {invariant}")

    if not adapter.is_file():
        raise SystemExit("[framework-translation] FrameworkTypeAdapter source missing")
    adapter_text = adapter.read_text(encoding="utf-8")
    for invariant in [
        "ParceledListSliceCompat.isReturnParceledListSlice(method)",
        "method.getReturnType()",
        "platform-fallback",
        "type-safe-default",
        "guestTargetSdk()",
    ]:
        if invariant not in adapter_text:
            raise SystemExit(f"[framework-translation] adapter verification failed: {invariant}")

    print("[framework-translation] runtime framework translation verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_framework_translation.py <NewBlackbox-root>")

    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        raise SystemExit(f"[framework-translation] engine root missing: {root}")

    patch_build_compat(root)
    write_type_adapter(root)
    patch_dispatcher(root)
    verify(root)


if __name__ == "__main__":
    main()
