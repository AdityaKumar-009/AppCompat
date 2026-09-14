#!/usr/bin/env python3
"""Harden post-EULA legacy special-access and service-discovery gates.

Old utilities commonly advance from an EULA directly into two APIs that modern
Android virtualisation engines must represent truthfully:

* Settings.canDrawOverlays() ultimately asks AppOps about SYSTEM_ALERT_WINDOW.
  AppCompat already translates special AppOps onto the helper identity, but the
  original bridge re-queries ServiceManager through getWho() after the appops Binder
  has been replaced by our proxy. On modern releases that can resolve back to the
  proxy itself. Route special-access checks through BinderInvocationStub's preserved
  original base interface instead, and recognize OP_SYSTEM_ALERT_WINDOW numerically
  even if hidden AppOps name reflection is unavailable on a vendor build.
* ActivityManager.getRunningServices() is deprecated/restricted on modern Android.
  BlackBox already owns authoritative virtual service records, so synthesize the
  guest-facing RunningServiceInfo list directly from those records instead of asking
  the host ActivityManager and trying to map host PIDs back afterward.

There are intentionally no package-name checks for any particular guest app.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[legacy-service-gate] {label}: already applied")
            return text
        raise SystemExit(f"[legacy-service-gate] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[legacy-service-gate] {label}: expected one match, found {count}")
    print(f"[legacy-service-gate] {label}: applied")
    return text.replace(old, new, 1)


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

    # patch_special_access_translation.py has already inserted this branch. Keep its
    # generic special-access classifier, but make the overlay gate independent of
    # hidden op-name reflection and invoke the preserved original Binder interface.
    old = '''        if (LegacySpecialAccessCompat.isSpecialAccessAppOp(args)) {
            MethodParameterUtils.replaceAllAppPkg(args);
            MethodParameterUtils.replaceFirstUid(args);
            MethodParameterUtils.replaceLastUid(args);
            return method.invoke(getWho(), args);
        }'''
    new = '''        if (containsSystemAlertWindowOp(args)
                || LegacySpecialAccessCompat.isSpecialAccessAppOp(args)) {
            MethodParameterUtils.replaceAllAppPkg(args);
            MethodParameterUtils.replaceFirstUid(args);
            MethodParameterUtils.replaceLastUid(args);
            try {
                // getWho() re-queries ServiceManager after this Binder service has
                // been replaced and can therefore resolve back into this proxy.
                // getBase() is the original IAppOpsService captured by
                // BinderInvocationStub before injection, so this crosses to the real
                // Android service exactly once instead of recursively re-entering us.
                Object result = method.invoke(getBase(), args);
                if (containsSystemAlertWindowOp(args)) {
                    Slog.d(TAG, "SYSTEM_ALERT_WINDOW AppOps delegated to original helper Binder: "
                            + methodName + " -> " + result);
                }
                return result;
            } catch (java.lang.reflect.InvocationTargetException failure) {
                Throwable cause = failure.getCause();
                if (cause != null) throw cause;
                throw failure;
            }
        }'''
    text = replace_once(
        text,
        old,
        new,
        "route special AppOps through original Binder base",
    )

    marker = '''    @Override
    public boolean isBadEnv() {'''
    helper = '''    // AppOpsManager.OP_SYSTEM_ALERT_WINDOW is stable framework op 24. Use the
    // numeric value here because opToName/opToPublicName can be hidden or vendor-
    // restricted even though Settings.canDrawOverlays() still sends the integer op.
    private static boolean containsSystemAlertWindowOp(Object[] args) {
        if (args == null) return false;
        for (Object arg : args) {
            if (arg instanceof Integer && ((Integer) arg).intValue() == 24) {
                return true;
            }
        }
        return false;
    }

'''
    if "private static boolean containsSystemAlertWindowOp" not in text:
        if marker not in text:
            raise SystemExit("[legacy-service-gate] AppOps helper insertion point not found")
        text = text.replace(marker, helper + marker, 1)
        print("[legacy-service-gate] deterministic overlay AppOps classifier: applied")

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
        (appops, "LegacySpecialAccessCompat.isSpecialAccessAppOp(args)"),
        (appops, "containsSystemAlertWindowOp"),
        (appops, "method.invoke(getBase(), args)"),
        (appops, "SYSTEM_ALERT_WINDOW AppOps delegated to original helper Binder"),
        (services, "new ActivityManager.RunningServiceInfo()"),
        (services, "running.service = new ComponentName(serviceInfo.packageName, serviceInfo.name)"),
        (services, "running.uid = processRecord.buid"),
    ]
    for source, invariant in required:
        if invariant not in source:
            raise SystemExit(f"[legacy-service-gate] verification failed: {invariant}")

    if "return method.invoke(getWho(), args);" in appops:
        raise SystemExit(
            "[legacy-service-gate] recursive-prone special AppOps getWho() invocation still present"
        )
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
