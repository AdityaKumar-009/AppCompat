#!/usr/bin/env python3
"""Harden virtual NotificationListenerService lifecycle ordering.

The real helper owns Android's notification-listener grant. Granting that access must
not eagerly create an old guest's listener/background overlay service while Settings
is still returning to onboarding. NotificationAccessProxyService therefore starts a
guest lazily on the first real notification. This patch guarantees that the guest's
onListenerConnected() callback is delivered exactly once before its first posted or
removed callback, and that disconnect resets that logical connection state.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[notification-lifecycle] {label}: already applied")
            return text
        raise SystemExit(f"[notification-lifecycle] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[notification-lifecycle] {label}: expected one match, found {count}")
    print(f"[notification-lifecycle] {label}: applied")
    return text.replace(old, new, 1)


def patch_dispatcher(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/app/dispatcher/AppServiceDispatcher.java"
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "import java.util.Map;",
        "import java.util.Map;\nimport java.util.WeakHashMap;",
        "import weak service-state map",
    )

    text = replace_once(
        text,
        "    private Map<Intent.FilterComparison, ServiceRecord> mService = new HashMap<>();",
        "    private Map<Intent.FilterComparison, ServiceRecord> mService = new HashMap<>();\n"
        "    private final Map<NotificationListenerService, Boolean> mNotificationListenerConnected =\n"
        "            new WeakHashMap<>();",
        "track virtual listener connection state",
    )

    old = r'''    /**
     * A virtual NotificationListenerService cannot be bound by Android directly.
     * The real helper listener forwards callbacks as explicit virtual service starts;
     * translate those bridge starts back into the callbacks legacy code expects.
     */
    private int dispatchSpecialAccessCallback(Service service, Intent intent) {
        if (!(service instanceof NotificationListenerService) || intent == null) {
            return Integer.MIN_VALUE;
        }
        String action = intent.getAction();
        if (action == null || !action.startsWith("com.appcompat.engine.special.")) {
            return Integer.MIN_VALUE;
        }
        NotificationListenerService listener = (NotificationListenerService) service;
        try {
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_LISTENER_CONNECTED.equals(action)) {
                listener.onListenerConnected();
                return START_NOT_STICKY;
            }
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_LISTENER_DISCONNECTED.equals(action)) {
                listener.onListenerDisconnected();
                return START_NOT_STICKY;
            }
            StatusBarNotification sbn = intent.getParcelableExtra(
                    LegacySpecialAccessCompat.EXTRA_STATUS_BAR_NOTIFICATION);
            if (sbn == null) {
                return START_NOT_STICKY;
            }
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_POSTED.equals(action)) {
                listener.onNotificationPosted(sbn);
                return START_NOT_STICKY;
            }
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_REMOVED.equals(action)) {
                listener.onNotificationRemoved(sbn);
                return START_NOT_STICKY;
            }
        } catch (Throwable callbackFailure) {
            callbackFailure.printStackTrace();
            return START_NOT_STICKY;
        }
        return Integer.MIN_VALUE;
    }'''

    new = r'''    /**
     * A virtual NotificationListenerService cannot be bound by Android directly.
     * The real helper forwards callbacks as explicit virtual service starts. A guest
     * is materialized lazily on its first real notification, so ensure its connected
     * callback precedes posted/removed exactly once per real-listener connection.
     */
    private int dispatchSpecialAccessCallback(Service service, Intent intent) {
        if (!(service instanceof NotificationListenerService) || intent == null) {
            return Integer.MIN_VALUE;
        }
        String action = intent.getAction();
        if (action == null || !action.startsWith("com.appcompat.engine.special.")) {
            return Integer.MIN_VALUE;
        }
        NotificationListenerService listener = (NotificationListenerService) service;
        try {
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_LISTENER_CONNECTED.equals(action)) {
                ensureNotificationListenerConnected(listener);
                return START_NOT_STICKY;
            }
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_LISTENER_DISCONNECTED.equals(action)) {
                boolean wasConnected;
                synchronized (mNotificationListenerConnected) {
                    wasConnected = mNotificationListenerConnected.remove(listener) != null;
                }
                if (wasConnected) {
                    listener.onListenerDisconnected();
                }
                return START_NOT_STICKY;
            }
            StatusBarNotification sbn = intent.getParcelableExtra(
                    LegacySpecialAccessCompat.EXTRA_STATUS_BAR_NOTIFICATION);
            if (sbn == null) {
                return START_NOT_STICKY;
            }
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_POSTED.equals(action)) {
                ensureNotificationListenerConnected(listener);
                listener.onNotificationPosted(sbn);
                return START_NOT_STICKY;
            }
            if (LegacySpecialAccessCompat.ACTION_NOTIFICATION_REMOVED.equals(action)) {
                ensureNotificationListenerConnected(listener);
                listener.onNotificationRemoved(sbn);
                return START_NOT_STICKY;
            }
        } catch (Throwable callbackFailure) {
            callbackFailure.printStackTrace();
            return START_NOT_STICKY;
        }
        return Integer.MIN_VALUE;
    }

    private void ensureNotificationListenerConnected(NotificationListenerService listener) {
        synchronized (mNotificationListenerConnected) {
            if (Boolean.TRUE.equals(mNotificationListenerConnected.get(listener))) {
                return;
            }
            listener.onListenerConnected();
            mNotificationListenerConnected.put(listener, Boolean.TRUE);
        }
    }'''

    text = replace_once(text, old, new, "order lazy notification-listener callbacks")
    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/app/dispatcher/AppServiceDispatcher.java"
    text = path.read_text(encoding="utf-8")
    for invariant in (
        "WeakHashMap",
        "mNotificationListenerConnected",
        "ensureNotificationListenerConnected(listener)",
        "listener.onNotificationPosted(sbn)",
        "listener.onNotificationRemoved(sbn)",
        "mNotificationListenerConnected.remove(listener)",
    ):
        if invariant not in text:
            raise SystemExit(f"[notification-lifecycle] verification failed: {invariant}")
    print("[notification-lifecycle] lazy virtual listener lifecycle verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_notification_listener_lifecycle.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    patch_dispatcher(root)
    verify(root)


if __name__ == "__main__":
    main()
