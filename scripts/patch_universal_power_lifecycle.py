#!/usr/bin/env python3
"""Restore helper-owned PowerManager wakelock semantics for virtual legacy apps.

Upstream BlackBox returns success from acquire/release/update wakelock calls without
actually touching PowerManager.  That silently breaks alarms, players, downloads,
navigation, BLE/sensor loggers and long-running legacy services.

A wakelock is a safe identity translation: the real helper UID is the process actually
executing guest code, so Android can own the lock under that UID. WorkSource attribution
is stripped because arbitrary virtual UIDs do not exist in system_server and passing a
non-null WorkSource can require UPDATE_DEVICE_STATS; keeping the lock itself is the
correct third-party-compatible fallback.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_universal_power_lifecycle.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IPowerManagerProxy.java"
    text = path.read_text(encoding="utf-8")

    text = text.replace("import android.content.Context;\n", "import android.content.Context;\nimport android.os.WorkSource;\n\nimport java.lang.reflect.Method;\n", 1)
    text = text.replace(
        "import top.niunaijun.blackbox.fake.service.base.ValueMethodProxy;\n",
        "import top.niunaijun.blackbox.utils.MethodParameterUtils;\n",
        1,
    )

    old = '''    @Override
    protected void onBindMethod() {
        super.onBindMethod();
        addMethodHook(new ValueMethodProxy("acquireWakeLock", 0));
        addMethodHook(new ValueMethodProxy("acquireWakeLockWithUid", 0));
        addMethodHook(new ValueMethodProxy("releaseWakeLock", 0));
        addMethodHook(new ValueMethodProxy("updateWakeLockWorkSource", 0));
        addMethodHook(new ValueMethodProxy("isWakeLockLevelSupported", true));
    }'''
    new = '''    @Override
    protected void onBindMethod() {
        // Deliberately no fake ValueMethodProxy hooks. The helper process is the real
        // kernel/process owner of a guest wakelock, so forward to Android.
        super.onBindMethod();
    }

    @Override
    public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
        MethodParameterUtils.replaceAllAppPkg(args);
        MethodParameterUtils.replaceFirstUid(args);

        // Virtual UIDs cannot be represented in system_server WorkSource accounting,
        // and third-party callers normally do not have UPDATE_DEVICE_STATS anyway.
        if (args != null) {
            for (int i = 0; i < args.length; i++) {
                if (args[i] instanceof WorkSource) {
                    args[i] = null;
                }
            }
        }
        return super.invoke(proxy, method, args);
    }'''
    count = text.count(old)
    if count != 1:
        if new not in text:
            raise SystemExit(f"[universal-power] expected one wakelock no-op block, found {count}")
    else:
        text = text.replace(old, new, 1)
        print("[universal-power] real helper-owned wakelocks: applied")

    required = (
        "MethodParameterUtils.replaceAllAppPkg(args);",
        "MethodParameterUtils.replaceFirstUid(args);",
        "args[i] instanceof WorkSource",
        "return super.invoke(proxy, method, args);",
    )
    for item in required:
        if item not in text:
            raise SystemExit(f"[universal-power] verification failed: {item}")
    if 'ValueMethodProxy("acquireWakeLock"' in text or 'ValueMethodProxy("releaseWakeLock"' in text:
        raise SystemExit("[universal-power] wakelock no-op hook remains")

    path.write_text(text, encoding="utf-8")
    print("[universal-power] wakelock lifecycle translation verified")


if __name__ == "__main__":
    main()
