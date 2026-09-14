#!/usr/bin/env python3
"""Harden ordinary legacy onboarding/dialog paths on modern Android hosts.

This patch runs after the existing package/window compatibility layers.  It fixes two
failure modes that are especially common in old apps whose first interactive action
opens an AlertDialog:

1. BlackBox's PackageManagerCompat wrapper swallows every Throwable raised while
   materialising PackageInfo and returns null.  A proxy-side retry with flags=0 is
   therefore not enough: even base metadata can still collapse to null on a newer
   framework/vendor build.  Preserve the installed-package contract by returning a
   minimal PackageInfo (package/version/requested permissions) when rich generation
   fails.
2. Dialogs are separate application windows.  Some modern framework/vendor paths can
   lose the activity token while a virtual activity itself still renders normally.
   Remember only a token that system_server has already accepted for a top-level app
   window, fill a missing token for later application/sub-windows, and retry only the
   explicit BAD_APP_TOKEN/BAD_SUBWINDOW_TOKEN/NOT_APP_TOKEN result codes.  Never
   replace a successful/non-null token pre-emptively.

The changes are generic; there are no Floatify package-name checks.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[legacy-dialog] {label}: already applied")
            return text
        raise SystemExit(f"[legacy-dialog] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[legacy-dialog] {label}: expected one match, found {count}")
    print(f"[legacy-dialog] {label}: applied")
    return text.replace(old, new, 1)


def patch_package_info_fallback(root: Path) -> None:
    path = root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/core/system/pm/"
        "PackageManagerCompat.java"
    )
    text = path.read_text(encoding="utf-8")

    old = '''    public static PackageInfo generatePackageInfo(BPackageSettings ps, int flags, BPackageUserState state, int userId) {
        if (ps == null) {
            return null;
        }
        BPackage p = ps.pkg;
        if (p != null) {
            PackageInfo packageInfo = null;
            try {
                packageInfo = generatePackageInfo(p, flags, 0, 0, state, userId);
            } catch (Throwable ignored) {
            }
            return packageInfo;
        }
        return null;
    }
'''
    new = '''    public static PackageInfo generatePackageInfo(BPackageSettings ps, int flags, BPackageUserState state, int userId) {
        if (ps == null || ps.pkg == null) {
            return null;
        }
        BPackage p = ps.pkg;
        Throwable richFailure = null;
        try {
            PackageInfo packageInfo = generatePackageInfo(p, flags, 0, 0, state, userId);
            if (packageInfo != null) {
                return packageInfo;
            }
        } catch (Throwable failure) {
            richFailure = failure;
            android.util.Log.w("PackageManagerCompat",
                    "Rich PackageInfo generation failed for " + p.packageName
                            + " flags=0x" + Integer.toHexString(flags), failure);
        }

        // An installed, visible virtual package must still expose its identity and
        // version metadata even if a newer framework/vendor field breaks rich
        // component materialisation.  Old EULA/changelog code commonly requests
        // GET_ACTIVITIES only to read versionCode/versionName immediately.
        PackageInfo minimal = generateMinimalPackageInfo(p, state, userId);
        if (minimal != null) {
            android.util.Log.w("PackageManagerCompat",
                    "Using minimal PackageInfo fallback for " + p.packageName
                            + " flags=0x" + Integer.toHexString(flags)
                            + (richFailure != null ? " after exception" : " after null result"));
        }
        return minimal;
    }

    private static PackageInfo generateMinimalPackageInfo(BPackage p,
                                                            BPackageUserState state,
                                                            int userId) {
        if (p == null || state == null || !state.installed || state.hidden) {
            return null;
        }
        try {
            PackageInfo pi = new PackageInfo();
            pi.packageName = p.packageName;
            pi.versionCode = p.mVersionCode;
            pi.versionName = p.mVersionName;
            pi.sharedUserId = p.mSharedUserId;
            pi.sharedUserLabel = p.mSharedUserLabel;

            // ApplicationInfo is useful to callers, but it is deliberately
            // best-effort here because it is one of the framework-sensitive pieces
            // that may have caused rich PackageInfo generation to fail.
            try {
                pi.applicationInfo = generateApplicationInfo(p, 0, state, userId);
            } catch (Throwable appInfoFailure) {
                android.util.Log.w("PackageManagerCompat",
                        "Minimal ApplicationInfo generation also failed for " + p.packageName,
                        appInfoFailure);
            }

            if (p.requestedPermissions != null && !p.requestedPermissions.isEmpty()) {
                String[] requested = new String[p.requestedPermissions.size()];
                p.requestedPermissions.toArray(requested);
                pi.requestedPermissions = requested;
            }
            return pi;
        } catch (Throwable fallbackFailure) {
            android.util.Log.e("PackageManagerCompat",
                    "Minimal PackageInfo fallback failed for " + p.packageName,
                    fallbackFailure);
            return null;
        }
    }
'''
    text = replace_once(
        text,
        old,
        new,
        "preserve minimal PackageInfo when rich generation fails",
    )
    path.write_text(text, encoding="utf-8")


def patch_application_window_tokens(root: Path) -> None:
    path = root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/"
        "IWindowSessionProxy.java"
    )
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "import android.os.Process;\nimport android.os.Build;",
        "import android.os.Process;\nimport android.os.IBinder;\nimport android.os.Build;",
        "import application-window token type",
    )

    text = replace_once(
        text,
        '''    private IInterface mSession;
''',
        '''    private IInterface mSession;

    // Per guest process: remember only a token after system_server has accepted a
    // top-level application window using it.  This makes it a safe fallback for a
    // later Dialog/Popup whose virtual WindowManager lost its default activity token.
    private static volatile IBinder sLastAcceptedApplicationToken;
''',
        "remember last accepted application token",
    )

    old_hook = '''        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            WindowManager.LayoutParams params = prepareLayoutParams(args);
            if (params == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.O
                    || !isLegacyAlertType(params.type)) {
                return method.invoke(who, args);
            }

            final int originalType = params.type;
            // Android O+ may *accept* these pre-O alert types while assigning them
            // deliberately downgraded layering. A success result therefore does not
            // mean the old semantic survived. Translate before crossing into
            // system_server so legacy overlays receive Android's modern SAW type.
            params.type = WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY;
            Slog.d(TAG, "Translating legacy alert type " + originalType
                    + " to TYPE_APPLICATION_OVERLAY before addToDisplay");
            return method.invoke(who, args);
        }
'''
    new_hook = '''        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            WindowManager.LayoutParams params = prepareLayoutParams(args);
            if (params == null) {
                return method.invoke(who, args);
            }

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                    && isLegacyAlertType(params.type)) {
                final int originalType = params.type;
                // Android O+ may accept these pre-O alert types with deliberately
                // downgraded layering. Translate before crossing into system_server.
                params.type = WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY;
                Slog.d(TAG, "Translating legacy alert type " + originalType
                        + " to TYPE_APPLICATION_OVERLAY before addToDisplay");
                return method.invoke(who, args);
            }

            return addApplicationWindowWithTokenFallback(who, method, args, params);
        }
'''
    text = replace_once(
        text,
        old_hook,
        new_hook,
        "recover missing/rejected application-window tokens",
    )

    marker = '''    private static WindowManager.LayoutParams prepareLayoutParams(Object[] args) {'''
    helpers = '''    private static Object addApplicationWindowWithTokenFallback(Object who,
                                                                  Method method,
                                                                  Object[] args,
                                                                  WindowManager.LayoutParams params)
            throws Throwable {
        if (!isApplicationOrSubWindow(params.type)) {
            return method.invoke(who, args);
        }

        final IBinder acceptedFallback = sLastAcceptedApplicationToken;
        if (params.token == null && acceptedFallback != null) {
            params.token = acceptedFallback;
            Slog.w(TAG, "Restored missing application window token for type=" + params.type);
        }

        Object first = method.invoke(who, args);
        if (isBadApplicationTokenResult(first)
                && acceptedFallback != null
                && params.token != acceptedFallback) {
            IBinder rejected = params.token;
            params.token = acceptedFallback;
            Slog.w(TAG, "Application window token was rejected for type=" + params.type
                    + "; retrying with last system_server-accepted activity token");
            Object retry = method.invoke(who, args);
            if (isRejectedAddResult(retry)) {
                // Preserve the original caller state if recovery also fails.
                params.token = rejected;
                return first;
            }
            first = retry;
        }

        if (!isRejectedAddResult(first)
                && isTopLevelApplicationWindow(params.type)
                && params.token != null) {
            sLastAcceptedApplicationToken = params.token;
        }
        return first;
    }

    private static boolean isTopLevelApplicationWindow(int type) {
        return type >= WindowManager.LayoutParams.FIRST_APPLICATION_WINDOW
                && type <= WindowManager.LayoutParams.LAST_APPLICATION_WINDOW;
    }

    private static boolean isApplicationOrSubWindow(int type) {
        return isTopLevelApplicationWindow(type)
                || (type >= WindowManager.LayoutParams.FIRST_SUB_WINDOW
                && type <= WindowManager.LayoutParams.LAST_SUB_WINDOW);
    }

    private static boolean isBadApplicationTokenResult(Object value) {
        if (!(value instanceof Number)) return false;
        int result = ((Number) value).intValue();
        // WindowManagerGlobal.ADD_BAD_APP_TOKEN / ADD_BAD_SUBWINDOW_TOKEN /
        // ADD_NOT_APP_TOKEN.  Restrict recovery to token-specific failures only.
        return result == -1 || result == -2 || result == -3;
    }

'''
    if "addApplicationWindowWithTokenFallback" not in text.split(marker)[0]:
        if marker not in text:
            raise SystemExit("[legacy-dialog] window helper insertion point not found")
        text = text.replace(marker, helpers + marker, 1)
        print("[legacy-dialog] application-window token helpers: applied")

    old_relayout = '''                if (arg instanceof WindowManager.LayoutParams) {
                    WindowManager.LayoutParams lp = (WindowManager.LayoutParams) arg;
                    if (BlackBoxCore.get().isDisableFlagSecure()) {
                        lp.flags &= ~WindowManager.LayoutParams.FLAG_SECURE;
                    }
                }
'''
    new_relayout = '''                if (arg instanceof WindowManager.LayoutParams) {
                    WindowManager.LayoutParams lp = (WindowManager.LayoutParams) arg;
                    lp.packageName = BlackBoxCore.getHostPkg();
                    if (BlackBoxCore.get().isDisableFlagSecure()) {
                        lp.flags &= ~WindowManager.LayoutParams.FLAG_SECURE;
                    }
                }
'''
    text = replace_once(
        text,
        old_relayout,
        new_relayout,
        "keep host package identity during relayout",
    )

    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    pm = (root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/core/system/pm/"
        "PackageManagerCompat.java"
    )).read_text(encoding="utf-8")
    window = (root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/"
        "IWindowSessionProxy.java"
    )).read_text(encoding="utf-8")

    required = [
        (pm, "generateMinimalPackageInfo"),
        (pm, "Using minimal PackageInfo fallback"),
        (pm, "pi.versionCode = p.mVersionCode"),
        (window, "sLastAcceptedApplicationToken"),
        (window, "addApplicationWindowWithTokenFallback"),
        (window, "isBadApplicationTokenResult"),
        (window, "Restored missing application window token"),
        (window, "lp.packageName = BlackBoxCore.getHostPkg()"),
    ]
    for source, invariant in required:
        if invariant not in source:
            raise SystemExit(f"[legacy-dialog] verification failed: {invariant}")

    print("[legacy-dialog] legacy onboarding/dialog hardening verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_legacy_dialog_hardening.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    patch_package_info_fallback(root)
    patch_application_window_tokens(root)
    verify(root)


if __name__ == "__main__":
    main()
