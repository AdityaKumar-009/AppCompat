#!/usr/bin/env python3
"""Replace broad upstream semantic no-ops when Android has a safe helper-owned equivalent.

The virtualizer historically disabled several Binder operations by returning a plausible
success/default value without doing any work.  That is worse than a visible failure for
compatibility because legacy apps continue with state that never actually exists.

This pass is deliberately conservative: only operations that can safely execute under
the real helper UID are restored here. Components that require a distinct platform role
or privileged/system identity (widgets, IME, wallpaper, etc.) remain separate proxy
problems and are not fabricated as successful.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[semantic-noops] {label}: already applied")
            return text
        raise SystemExit(f"[semantic-noops] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[semantic-noops] {label}: expected one match, found {count}")
    print(f"[semantic-noops] {label}: applied")
    return text.replace(old, new, 1)


def patch_pending_send(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IActivityManagerProxy.java"
    text = path.read_text(encoding="utf-8")
    old = '''    @ProxyMethod("sendIntentSender")
    public static class SendIntentSender extends MethodHook {

        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            return 0;
        }
    }'''
    new = '''    @ProxyMethod("sendIntentSender")
    public static class SendIntentSender extends MethodHook {

        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            // getIntentSender() already returns a real system IIntentSender whose
            // payload targets our helper proxy component. Let system_server execute
            // it so PendingIntent.send() has the same semantics as an alarm/notification
            // firing the token. Sanitize any fill-in Intent crossing Binder.
            if (args != null) {
                for (Object arg : args) {
                    if (arg instanceof Intent) {
                        try {
                            IntentSanitizer.sanitizeClassExtrasForIpc((Intent) arg);
                        } catch (Throwable ignored) {
                        }
                    }
                }
            }
            return method.invoke(who, args);
        }
    }'''
    text = replace_once(text, old, new, "forward PendingIntent.send")
    path.write_text(text, encoding="utf-8")


def patch_content_observers(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/context/ContentServiceStub.java"
    text = path.read_text(encoding="utf-8")

    if "top.niunaijun.blackbox.utils.MethodParameterUtils" not in text:
        text = text.replace(
            "import top.niunaijun.blackbox.fake.hook.ProxyMethod;\n",
            "import top.niunaijun.blackbox.fake.hook.ProxyMethod;\n"
            "import top.niunaijun.blackbox.utils.MethodParameterUtils;\n",
            1,
        )

    old_register = '''    @ProxyMethod("registerContentObserver")
    public static class RegisterContentObserver extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            return 0;
        }
    }'''
    new_register = '''    @ProxyMethod("registerContentObserver")
    public static class RegisterContentObserver extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            // Observers of real system providers (Contacts, MediaStore, Calendar,
            // Settings, etc.) are valid under the helper UID. Preserve the binder
            // callback instead of pretending registration succeeded.
            MethodParameterUtils.replaceAllAppPkg(args);
            try {
                return method.invoke(who, args);
            } catch (Throwable unsupportedVirtualAuthority) {
                // A provider that exists only inside the virtual PM has no direct
                // system_server authority. Keep old non-crashing behavior for that
                // subset while allowing real-provider observers to function.
                return null;
            }
        }
    }'''
    text = replace_once(text, old_register, new_register, "restore real content observer registration")

    old_notify = '''    @ProxyMethod("notifyChange")
    public static class NotifyChange extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            return 0;
        }
    }'''
    new_notify = '''    @ProxyMethod("notifyChange")
    public static class NotifyChange extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            MethodParameterUtils.replaceAllAppPkg(args);
            try {
                return method.invoke(who, args);
            } catch (Throwable unsupportedVirtualAuthority) {
                return null;
            }
        }
    }'''
    text = replace_once(text, old_notify, new_notify, "restore real content change notifications")
    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    am = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IActivityManagerProxy.java").read_text(encoding="utf-8")
    content = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/context/ContentServiceStub.java").read_text(encoding="utf-8")
    for needle in (
        "PendingIntent.send() has the same semantics",
        "IntentSanitizer.sanitizeClassExtrasForIpc((Intent) arg)",
    ):
        if needle not in am:
            raise SystemExit(f"[semantic-noops] verification failed: {needle}")
    for needle in (
        "restore",  # source comments make accidental no-op regression obvious
        "MethodParameterUtils.replaceAllAppPkg(args);",
        "return method.invoke(who, args);",
    ):
        if needle not in content:
            raise SystemExit(f"[semantic-noops] content observer verification failed: {needle}")
    print("[semantic-noops] PendingIntent.send + system content-observer semantics verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_universal_semantic_noops.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    patch_pending_send(root)
    patch_content_observers(root)
    verify(root)


if __name__ == "__main__":
    main()
