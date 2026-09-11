#!/usr/bin/env python3
"""Bridge framework services that authorize special access by package/component.

Opening the correct Settings page is only half of compatibility. Several framework
services subsequently send the guest's virtual package or listener ComponentName to
system_server. Rewrite those identities to the real AppCompat helper only at the
system boundary so the user's actual Settings grant is honored.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[special-service] {label}: already applied")
            return text
        raise SystemExit(f"[special-service] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[special-service] {label}: expected one match, found {count}")
    print(f"[special-service] {label}: applied")
    return text.replace(old, new, 1)


def write_usage_stats_mirror(root: Path) -> None:
    path = root / "Bcore/src/main/java/black/android/app/usage/IUsageStatsManager.java"
    source = '''package black.android.app.usage;

import android.os.IBinder;
import android.os.IInterface;

import top.niunaijun.blackreflection.annotation.BClassName;
import top.niunaijun.blackreflection.annotation.BStaticMethod;

@BClassName("android.app.usage.IUsageStatsManager")
public interface IUsageStatsManager {
    @BClassName("android.app.usage.IUsageStatsManager$Stub")
    interface Stub {
        @BStaticMethod
        IInterface asInterface(IBinder binder);
    }
}
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    print("[special-service] IUsageStatsManager mirror: written")


def write_usage_stats_proxy(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IUsageStatsManagerProxy.java"
    source = '''package top.niunaijun.blackbox.fake.service;

import android.content.Context;

import java.lang.reflect.Method;

import black.android.app.usage.BRIUsageStatsManagerStub;
import black.android.os.BRServiceManager;
import top.niunaijun.blackbox.fake.hook.BinderInvocationStub;
import top.niunaijun.blackbox.utils.MethodParameterUtils;

/**
 * UsageStatsManager includes Context.getOpPackageName() in its Binder calls. A
 * virtual package is unknown to system_server, even after the user enabled Usage
 * access for AppCompat Runtime. Translate only installed guest package arguments to
 * the real helper package; interval/user/time arguments remain untouched.
 */
public class IUsageStatsManagerProxy extends BinderInvocationStub {
    public IUsageStatsManagerProxy() {
        super(BRServiceManager.get().getService(Context.USAGE_STATS_SERVICE));
    }

    @Override
    protected Object getWho() {
        return BRIUsageStatsManagerStub.get().asInterface(
                BRServiceManager.get().getService(Context.USAGE_STATS_SERVICE));
    }

    @Override
    protected void inject(Object baseInvocation, Object proxyInvocation) {
        replaceSystemService(Context.USAGE_STATS_SERVICE);
    }

    @Override
    public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
        MethodParameterUtils.replaceAllAppPkg(args);
        return super.invoke(proxy, method, args);
    }

    @Override
    public boolean isBadEnv() {
        return false;
    }
}
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    print("[special-service] IUsageStatsManagerProxy: written")


def patch_hook_manager(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/hook/HookManager.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.fake.service.IStorageStatsManagerProxy;\n"
        "import top.niunaijun.blackbox.fake.service.ISystemUpdateProxy;",
        "import top.niunaijun.blackbox.fake.service.IStorageStatsManagerProxy;\n"
        "import top.niunaijun.blackbox.fake.service.IUsageStatsManagerProxy;\n"
        "import top.niunaijun.blackbox.fake.service.ISystemUpdateProxy;",
        "import UsageStats service bridge",
    )
    text = replace_once(
        text,
        "            addInjector(new IAppOpsManagerProxy());\n"
        "            addInjector(new INotificationManagerProxy());",
        "            addInjector(new IAppOpsManagerProxy());\n"
        "            addInjector(new IUsageStatsManagerProxy());\n"
        "            addInjector(new INotificationManagerProxy());",
        "install UsageStats service bridge",
    )
    path.write_text(text, encoding="utf-8")


def patch_media_session(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IMediaSessionManagerProxy.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import android.content.Context;",
        "import android.content.ComponentName;\nimport android.content.Context;",
        "import ComponentName for media-session listener attribution",
    )
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.fake.hook.ProxyMethod;",
        "import top.niunaijun.blackbox.fake.hook.ProxyMethod;\n"
        "import top.niunaijun.blackbox.fake.hook.ProxyMethods;\n"
        "import top.niunaijun.blackbox.utils.MethodParameterUtils;\n"
        "import top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat;",
        "import media-session special access helpers",
    )
    insertion = r'''

    /**
     * getActiveSessions()/active-session listeners authorize a NotificationListener
     * ComponentName in system_server. Replace a granted virtual listener with the
     * real AppCompat proxy component; otherwise keep Android's denial semantics.
     */
    @ProxyMethods({"getSessions", "addSessionsListener"})
    public static class NotificationListenerIdentity extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            if (args != null) {
                for (int i = 0; i < args.length; i++) {
                    if (!(args[i] instanceof ComponentName)) continue;
                    ComponentName component = (ComponentName) args[i];
                    if (LegacySpecialAccessCompat.isGuestComponent(component)
                            && LegacySpecialAccessCompat.isNotificationListenerAccessGrantedForGuest(
                                    component.getPackageName())) {
                        args[i] = new ComponentName(
                                BlackBoxCore.getHostPkg(),
                                "com.appcompat.engine.NotificationAccessProxyService");
                    }
                }
                MethodParameterUtils.replaceAllAppPkg(args);
            }
            return method.invoke(who, args);
        }
    }
'''
    if "class NotificationListenerIdentity" not in text:
        pos = text.rfind("\n}")
        if pos < 0:
            raise SystemExit("[special-service] media session closing brace not found")
        text = text[:pos] + insertion + text[pos:]
        print("[special-service] media-session listener attribution: applied")
    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    mirror = root / "Bcore/src/main/java/black/android/app/usage/IUsageStatsManager.java"
    proxy = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IUsageStatsManagerProxy.java"
    hooks = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/hook/HookManager.java").read_text(encoding="utf-8")
    media = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IMediaSessionManagerProxy.java").read_text(encoding="utf-8")
    if not mirror.is_file() or not proxy.is_file():
        raise SystemExit("[special-service] UsageStats bridge missing")
    if "addInjector(new IUsageStatsManagerProxy())" not in hooks:
        raise SystemExit("[special-service] UsageStats bridge not installed")
    if "NotificationListenerIdentity" not in media or "getSessions" not in media:
        raise SystemExit("[special-service] media-session listener bridge missing")
    print("[special-service] package/component service attribution verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_special_access_service_attribution.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    write_usage_stats_mirror(root)
    write_usage_stats_proxy(root)
    patch_hook_manager(root)
    patch_media_session(root)
    verify(root)


if __name__ == "__main__":
    main()
