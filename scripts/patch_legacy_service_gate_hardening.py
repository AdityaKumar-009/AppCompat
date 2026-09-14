#!/usr/bin/env python3
"""Harden post-EULA legacy special-access and service-discovery gates.

Old utilities commonly advance from an EULA directly into two APIs that modern
Android virtualisation engines must represent truthfully:

* Settings.canDrawOverlays() ultimately asks AppOps about SYSTEM_ALERT_WINDOW.
  The pinned engine historically returned MODE_ALLOWED for every check/note/start
  AppOps call.  Besides being untruthful, that is type-unsafe on newer Android where
  some note/start calls return objects rather than an int.  For the overlay gate,
  delegate the check to the real system AppOps service using the helper package/UID.
  Android Settings then remains the source of truth and can grant the capability to
  the real helper package that actually owns windows.
* ActivityManager.getRunningServices() is deprecated/restricted on modern Android.
  BlackBox already owns authoritative virtual service records, so synthesize the
  guest-facing RunningServiceInfo list directly from those records instead of asking
  the host ActivityManager and trying to map host PIDs back afterward.

There are intentionally no package-name checks for any particular guest app.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_between(text: str, start: str, end: str, replacement: str, label: str) -> str:
    start_i = text.find(start)
    if start_i < 0:
        if replacement.strip() in text:
            print(f"[legacy-service-gate] {label}: already applied")
            return text
        raise SystemExit(f"[legacy-service-gate] {label}: start marker not found")
    end_i = text.find(end, start_i)
    if end_i < 0:
        raise SystemExit(f"[legacy-service-gate] {label}: end marker not found")
    print(f"[legacy-service-gate] {label}: applied")
    return text[:start_i] + replacement + "\n\n" + text[end_i:]


def patch_truthful_overlay_appops(root: Path) -> None:
    path = root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/"
        "IAppOpsManagerProxy.java"
    )
    text = path.read_text(encoding="utf-8")

    if "import java.lang.reflect.InvocationTargetException;" not in text:
        text = text.replace(
            "import java.lang.reflect.Method;",
            "import java.lang.reflect.Method;\nimport java.lang.reflect.InvocationTargetException;",
            1,
        )

    start = '''    @Override
    public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {'''
    end = '''    @Override
    public boolean isBadEnv() {'''
    replacement = '''    @Override
    public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
        String methodName = method.getName();

        // SYSTEM_ALERT_WINDOW is a real special-app-access grant owned by the
        // helper package/UID. Never fabricate MODE_ALLOWED for this gate: doing so
        // makes a legacy app skip Android's overlay consent screen and immediately
        // attempt later onboarding/service work under a permission it does not have.
        if (methodName.startsWith("check") && containsSystemAlertWindowOp(args)) {
            rewriteSpecialAccessIdentity(args);
            try {
                Object result = method.invoke(getBase(), args);
                Slog.d(TAG, "Delegating SYSTEM_ALERT_WINDOW AppOps check to real helper: "
                        + methodName + " -> " + result);
                return result;
            } catch (Throwable failure) {
                Throwable cause = unwrapInvocationFailure(failure);
                Slog.w(TAG, "Real SYSTEM_ALERT_WINDOW AppOps check failed for "
                        + methodName + "; reporting denied", cause);
                // All current check* AppOps entry points used for OP_SYSTEM_ALERT_WINDOW
                // return an int mode. Reporting MODE_ERRORED is safer than lying that
                // the grant exists and lets Settings.canDrawOverlays() return false.
                return AppOpsManager.MODE_ERRORED;
            }
        }

        // Preserve the pinned engine's compatibility policy for unrelated AppOps.
        // This branch is intentionally after the truthful overlay gate above.
        if (methodName.startsWith("check") ||
            methodName.startsWith("note") ||
            methodName.startsWith("start")) {
            Slog.d(TAG, "AppOps invoke: Bypassing system for " + methodName + ", allowing operation");
            return AppOpsManager.MODE_ALLOWED;
        }

        if (methodName.startsWith("finish")) {
            Slog.d(TAG, "AppOps invoke: Bypassing system for " + methodName);
            return null;
        }

        try {
            MethodParameterUtils.replaceFirstAppPkg(args);
            MethodParameterUtils.replaceLastUid(args);
            return super.invoke(proxy, method, args);
        } catch (SecurityException e) {
            Slog.w(TAG, "AppOps invoke: SecurityException caught for " + methodName + ", allowing operation", e);
            return AppOpsManager.MODE_ALLOWED;
        } catch (Exception e) {
            Slog.e(TAG, "AppOps invoke: Error in method " + methodName, e);
            return AppOpsManager.MODE_ALLOWED;
        }
    }

    private static boolean containsSystemAlertWindowOp(Object[] args) {
        if (args == null) return false;
        for (Object arg : args) {
            if (arg instanceof Integer
                    && ((Integer) arg).intValue() == AppOpsManager.OP_SYSTEM_ALERT_WINDOW) {
                return true;
            }
        }
        return false;
    }

    private static void rewriteSpecialAccessIdentity(Object[] args) {
        if (args == null) return;
        String guestPackage = null;
        try {
            guestPackage = BActivityThread.getAppPackageName();
        } catch (Throwable ignored) {
        }
        String hostPackage = BlackBoxCore.getHostPkg();
        int guestUid = BlackBoxCore.getBUid();
        int hostUid = BlackBoxCore.getHostUid();

        for (int i = 0; i < args.length; i++) {
            Object arg = args[i];
            if (arg instanceof String && guestPackage != null
                    && guestPackage.equals(arg) && hostPackage != null) {
                args[i] = hostPackage;
            } else if (arg instanceof Integer && guestUid > 0 && hostUid > 0
                    && ((Integer) arg).intValue() == guestUid) {
                // Newer AppOps signatures can carry additional integer fields after
                // uid (for example device identifiers). Replace by value, not by
                // positional guesses, so those fields remain untouched.
                args[i] = hostUid;
            }
        }
    }

    private static Throwable unwrapInvocationFailure(Throwable failure) {
        Throwable current = failure;
        while (current instanceof InvocationTargetException
                && ((InvocationTargetException) current).getCause() != null) {
            current = ((InvocationTargetException) current).getCause();
        }
        return current;
    }'''

    text = replace_between(
        text, start, end, replacement,
        "delegate SYSTEM_ALERT_WINDOW AppOps to helper identity",
    )
    path.write_text(text, encoding="utf-8")


def patch_virtual_running_services(root: Path) -> None:
    path = root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/core/system/am/"
        "ActiveServices.java"
    )
    text = path.read_text(encoding="utf-8")

    start = '''    public RunningServiceInfo getRunningServiceInfo(String callerPackage, int userId) {'''
    end = '''    public IBinder peekService(Intent intent, String resolvedType, int userId) {'''
    replacement = '''    public RunningServiceInfo getRunningServiceInfo(String callerPackage, int userId) {
        RunningServiceInfo info = new RunningServiceInfo();

        // mRunningServiceRecords is the authoritative virtual service registry.
        // Do not depend on ActivityManager#getRunningServices(): modern Android
        // intentionally restricts/deprecates that host query and the host proxy PID
        // is not required to answer a guest asking about its own virtual services.
        synchronized (mRunningServiceRecords) {
            for (RunningServiceRecord value : mRunningServiceRecords.values()) {
                if (value == null || value.mServiceInfo == null) {
                    continue;
                }
                ServiceInfo serviceInfo = value.mServiceInfo;
                if (callerPackage != null && serviceInfo.packageName != null
                        && !callerPackage.equals(serviceInfo.packageName)) {
                    continue;
                }

                ProcessRecord processRecord = BProcessManagerService.get().findProcessRecord(
                        serviceInfo.packageName, serviceInfo.processName, userId);
                if (processRecord == null) {
                    continue;
                }

                ActivityManager.RunningServiceInfo running =
                        new ActivityManager.RunningServiceInfo();
                running.service = new ComponentName(serviceInfo.packageName, serviceInfo.name);
                running.process = processRecord.processName;
                running.pid = processRecord.pid;
                // Expose the guest-facing BlackBox UID rather than the helper's real
                // Android UID, matching the rest of the virtual package identity.
                running.uid = processRecord.buid;
                running.started = value.mStartId.get() > 0;
                running.clientCount = value.mBindCount.get();
                info.mRunningServiceInfoList.add(running);
            }
        }
        return info;
    }'''

    text = replace_between(
        text, start, end, replacement,
        "synthesize running services from virtual registry",
    )
    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    appops = (root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/"
        "IAppOpsManagerProxy.java"
    )).read_text(encoding="utf-8")
    services = (root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/core/system/am/"
        "ActiveServices.java"
    )).read_text(encoding="utf-8")

    required = [
        (appops, "containsSystemAlertWindowOp"),
        (appops, "AppOpsManager.OP_SYSTEM_ALERT_WINDOW"),
        (appops, "method.invoke(getBase(), args)"),
        (appops, "rewriteSpecialAccessIdentity"),
        (appops, "Delegating SYSTEM_ALERT_WINDOW AppOps check to real helper"),
        (services, "new ActivityManager.RunningServiceInfo()"),
        (services, "running.service = new ComponentName(serviceInfo.packageName, serviceInfo.name)"),
        (services, "running.uid = processRecord.buid"),
    ]
    for source, invariant in required:
        if invariant not in source:
            raise SystemExit(f"[legacy-service-gate] verification failed: {invariant}")

    if "manager.getRunningServices(Integer.MAX_VALUE)" in services:
        raise SystemExit(
            "[legacy-service-gate] host ActivityManager#getRunningServices dependency still present"
        )

    print("[legacy-service-gate] post-EULA special-access/service gates verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_legacy_service_gate_hardening.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    patch_truthful_overlay_appops(root)
    patch_virtual_running_services(root)
    verify(root)


if __name__ == "__main__":
    main()
