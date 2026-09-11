#!/usr/bin/env python3
"""Translate pre-Oreo alert-window requests at the real system boundary.

Old applications can still construct TYPE_PHONE/TYPE_SYSTEM_ALERT/etc. even when
running inside a virtual package. Android evaluates the final IWindowSession call
against the real helper UID and the modern system_server implementation. Preserve
the historical window type first; if modern Android rejects that type, retry the
same request as TYPE_APPLICATION_OVERLAY while keeping the helper package identity.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[legacy-window] {label}: already applied")
            return text
        raise SystemExit(f"[legacy-window] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[legacy-window] {label}: expected one match, found {count}")
    print(f"[legacy-window] {label}: applied")
    return text.replace(old, new, 1)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_legacy_window_translation.py <NewBlackbox-root>")

    root = Path(sys.argv[1]).resolve()
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IWindowSessionProxy.java"
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "import android.os.Process;\nimport android.view.WindowManager;",
        "import android.os.Process;\nimport android.os.Build;\nimport android.view.WindowManager;\n\nimport java.lang.reflect.InvocationTargetException;",
        "import legacy overlay translation dependencies",
    )
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.fake.hook.ProxyMethod;",
        "import top.niunaijun.blackbox.fake.hook.ProxyMethod;\nimport top.niunaijun.blackbox.utils.Slog;",
        "import translation diagnostics",
    )

    old_hook = '''    @ProxyMethod("addToDisplay")
    public static class AddToDisplay extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            for (Object arg : args) {
                if (arg == null) {
                    continue;
                }
                if (arg instanceof WindowManager.LayoutParams) {
                    WindowManager.LayoutParams lp = (WindowManager.LayoutParams) arg;
                    lp.packageName = BlackBoxCore.getHostPkg();
                    if (BlackBoxCore.get().isDisableFlagSecure()) {
                        lp.flags &= ~WindowManager.LayoutParams.FLAG_SECURE;
                    }
                }
            }
            return method.invoke(who, args);
        }
    }'''
    new_hook = '''    @ProxyMethod("addToDisplay")
    public static class AddToDisplay extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            WindowManager.LayoutParams params = prepareLayoutParams(args);
            if (params == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.O
                    || !isLegacyAlertType(params.type)) {
                return method.invoke(who, args);
            }

            final int originalType = params.type;
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
            }
        }
    }'''
    text = replace_once(text, old_hook, new_hook, "retry rejected legacy alert windows")

    marker = '''    @ProxyMethod("addToDisplayAsUser")
    public static class AddToDisplayAsUser extends AddToDisplay {'''
    helpers = r'''    private static WindowManager.LayoutParams prepareLayoutParams(Object[] args) {
        if (args == null) return null;
        for (Object arg : args) {
            if (!(arg instanceof WindowManager.LayoutParams)) continue;
            WindowManager.LayoutParams lp = (WindowManager.LayoutParams) arg;
            lp.packageName = BlackBoxCore.getHostPkg();
            if (BlackBoxCore.get().isDisableFlagSecure()) {
                lp.flags &= ~WindowManager.LayoutParams.FLAG_SECURE;
            }
            return lp;
        }
        return null;
    }

    private static boolean isLegacyAlertType(int type) {
        return type == WindowManager.LayoutParams.TYPE_PHONE
                || type == WindowManager.LayoutParams.TYPE_PRIORITY_PHONE
                || type == WindowManager.LayoutParams.TYPE_SYSTEM_ALERT
                || type == WindowManager.LayoutParams.TYPE_SYSTEM_OVERLAY
                || type == WindowManager.LayoutParams.TYPE_SYSTEM_ERROR;
    }

    /**
     * IWindowSession add methods return a negative ADD_* status on many releases.
     * Do not retry successful/neutral values; only a rejected legacy overlay is
     * eligible for representation translation.
     */
    private static boolean isRejectedAddResult(Object value) {
        return value instanceof Number && ((Number) value).intValue() < 0;
    }

    private static Throwable unwrapInvocation(Throwable failure) {
        Throwable current = failure;
        while (current instanceof InvocationTargetException
                && ((InvocationTargetException) current).getCause() != null) {
            current = ((InvocationTargetException) current).getCause();
        }
        return current;
    }

    private static boolean isWindowPermissionFailure(Throwable failure) {
        return failure instanceof SecurityException
                || failure instanceof IllegalArgumentException;
    }

'''
    if "private static boolean isLegacyAlertType" not in text:
        if marker not in text:
            raise SystemExit("[legacy-window] helper insertion point not found")
        text = text.replace(marker, helpers + marker, 1)
        print("[legacy-window] legacy alert helpers: applied")

    # Some vendor releases expose an addToDisplay* variant that is not covered by
    # the upstream explicit annotations. Normalize those variants before the base
    # dispatcher reaches system_server so they cannot bypass package/type mapping.
    old_invoke = '''    @Override
    public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
        if (method != null && "addToDisplayAsUser".equals(method.getName())) {
            rewriteRequestedUserId(args);
        }
        return super.invoke(proxy, method, args);
    }'''
    new_invoke = '''    @Override
    public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
        if (method != null && "addToDisplayAsUser".equals(method.getName())) {
            rewriteRequestedUserId(args);
        }
        if (method != null && method.getName().startsWith("addToDisplay")
                && !"addToDisplay".equals(method.getName())
                && !"addToDisplayAsUser".equals(method.getName())) {
            WindowManager.LayoutParams params = prepareLayoutParams(args);
            if (params != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                    && isLegacyAlertType(params.type)) {
                params.type = WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY;
            }
        }
        return super.invoke(proxy, method, args);
    }'''
    text = replace_once(text, old_invoke, new_invoke, "cover vendor addToDisplay variants")

    path.write_text(text, encoding="utf-8")

    verified = path.read_text(encoding="utf-8")
    for invariant in [
        "TYPE_APPLICATION_OVERLAY",
        "isLegacyAlertType",
        "isRejectedAddResult",
        "unwrapInvocation",
        "method.getName().startsWith(\"addToDisplay\")",
    ]:
        if invariant not in verified:
            raise SystemExit(f"[legacy-window] verification failed: {invariant}")
    print("[legacy-window] legacy WindowManager translation verified")


if __name__ == "__main__":
    main()
