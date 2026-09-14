#!/usr/bin/env python3
"""Restore legacy Android runtime semantics that the pinned sandbox currently drops.

This patch is intentionally app-agnostic.  It fixes platform/lifecycle contracts used by
large classes of old applications rather than adding package-name exceptions:

* AlarmManager.set* calls are forwarded to the real helper UID instead of being no-ops.
* PendingIntent.getBroadcast/getService/getForegroundService are represented by real
  helper components and dispatched back to the original virtual guest target.
* Manifest receivers cold-start their guest process, matching Android's normal receiver
  lifecycle instead of silently dropping broadcasts while the guest process is dead.
* BOOT_COMPLETED wakes the helper and is relayed into all matching virtual receivers so
  old alarm/reminder/automation apps can rebuild state after a device restart.
* Dynamic manifest-receiver registration is isolated per filter and cleaned up on
  reinstall, preventing one bad/protected filter or a reinstall from corrupting the
  entire guest broadcast registry.

Security boundary: this does not fabricate privileged/signature permissions.  Android
still owns user consent and real UID-level policy; the helper only translates identity
and lifecycle where an ordinary third-party app is allowed to perform the operation.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[universal-runtime] {label}: already applied")
            return text
        raise SystemExit(f"[universal-runtime] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[universal-runtime] {label}: expected one match, found {count}")
    print(f"[universal-runtime] {label}: applied")
    return text.replace(old, new, 1)


def patch_alarm_manager(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IAlarmManagerProxy.java"
    text = path.read_text(encoding="utf-8")

    if "top.niunaijun.blackbox.utils.MethodParameterUtils" not in text:
        text = text.replace(
            "import top.niunaijun.blackbox.fake.hook.ProxyMethod;\n",
            "import top.niunaijun.blackbox.fake.hook.ProxyMethod;\n"
            "import top.niunaijun.blackbox.utils.MethodParameterUtils;\n",
            1,
        )

    old = '''    @ProxyMethod("set")
    public static class Set extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            return 0;
        }
    }'''
    new = '''    @ProxyMethod("set")
    public static class Set extends MethodHook {
        @Override
        protected Object hook(Object who, Method method, Object[] args) throws Throwable {
            // The upstream sandbox swallowed every alarm. PendingIntent creation is
            // already translated onto the helper UID; let Android's real AlarmManager
            // own timing, Doze and wakeup semantics for that helper identity.
            MethodParameterUtils.replaceAllAppPkg(args);
            return method.invoke(who, args);
        }
    }

    @Override
    public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
        // Newer IAlarmManager methods also carry package/attribution strings (remove,
        // getNextAlarmClock, canScheduleExactAlarms, etc.). Never leak a virtual package
        // name into system_server, which only knows the real helper package.
        MethodParameterUtils.replaceAllAppPkg(args);
        return super.invoke(proxy, method, args);
    }'''
    text = replace_once(text, old, new, "forward real AlarmManager operations")
    path.write_text(text, encoding="utf-8")


def write_pending_components(root: Path) -> None:
    proxy_dir = root / "Bcore/src/main/java/top/niunaijun/blackbox/proxy"
    proxy_dir.mkdir(parents=True, exist_ok=True)

    receiver = r'''package top.niunaijun.blackbox.proxy;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.RemoteException;

import top.niunaijun.blackbox.BlackBoxCore;
import top.niunaijun.blackbox.entity.am.PendingResultData;
import top.niunaijun.blackbox.proxy.record.ProxyPendingRecord;
import top.niunaijun.blackbox.utils.IntentSanitizer;
import top.niunaijun.blackbox.utils.Slog;

/** Real PendingIntent broadcast endpoint that re-enters the virtual receiver graph. */
public class ProxyPendingReceiver extends BroadcastReceiver {
    private static final String TAG = "ProxyPendingReceiver";

    @Override
    public void onReceive(Context context, Intent proxyIntent) {
        ProxyPendingRecord record = ProxyPendingRecord.create(proxyIntent);
        Intent target = record.mTarget;
        if (target == null) return;
        try {
            target.setExtrasClassLoader(context.getClassLoader());
            IntentSanitizer.restoreSanitizedClassExtras(target, context.getClassLoader());
        } catch (Throwable ignored) {
        }

        PendingResult pending = goAsync();
        try {
            // scheduleBroadcastReceiver() now cold-starts missing guest processes.
            BlackBoxCore.getBActivityManager().scheduleBroadcastReceiver(
                    target, new PendingResultData(pending), record.mUserId);
        } catch (RemoteException e) {
            pending.finish();
        } catch (Throwable t) {
            Slog.e(TAG, "Could not dispatch virtual PendingIntent broadcast", t);
            pending.finish();
        }
    }
}
'''
    (proxy_dir / "ProxyPendingReceiver.java").write_text(receiver, encoding="utf-8")

    service = r'''package top.niunaijun.blackbox.proxy;

import android.app.Service;
import android.content.Intent;
import android.os.IBinder;

import androidx.annotation.Nullable;

import top.niunaijun.blackbox.BlackBoxCore;
import top.niunaijun.blackbox.proxy.record.ProxyPendingRecord;
import top.niunaijun.blackbox.utils.IntentSanitizer;
import top.niunaijun.blackbox.utils.Slog;

/** Real PendingIntent service endpoint that starts the original virtual service. */
public class ProxyPendingService extends Service {
    private static final String TAG = "ProxyPendingService";
    public static final String EXTRA_REQUIRE_FOREGROUND =
            "_B_|_P_require_foreground_";

    @Override
    public int onStartCommand(Intent proxyIntent, int flags, int startId) {
        try {
            ProxyPendingRecord record = ProxyPendingRecord.create(proxyIntent);
            Intent target = record.mTarget;
            if (target != null) {
                target.setExtrasClassLoader(getClassLoader());
                IntentSanitizer.restoreSanitizedClassExtras(target, getClassLoader());
                String resolvedType = target.resolveTypeIfNeeded(getContentResolver());
                boolean requireForeground = proxyIntent != null
                        && proxyIntent.getBooleanExtra(EXTRA_REQUIRE_FOREGROUND, false);
                BlackBoxCore.getBActivityManager().startService(
                        target, resolvedType, requireForeground, record.mUserId);
            }
        } catch (Throwable t) {
            Slog.e(TAG, "Could not dispatch virtual PendingIntent service", t);
        } finally {
            stopSelf(startId);
        }
        return START_NOT_STICKY;
    }

    @Nullable
    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
'''
    (proxy_dir / "ProxyPendingService.java").write_text(service, encoding="utf-8")
    print("[universal-runtime] PendingIntent receiver/service proxies: written")


def patch_pending_intents(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IActivityManagerProxy.java"
    text = path.read_text(encoding="utf-8")

    old = '''                switch (type) {
                    case ActivityManagerCompat.INTENT_SENDER_ACTIVITY:
                        Intent shadow = new Intent();
                        shadow.setComponent(new ComponentName(BlackBoxCore.getHostPkg(), ProxyManifest.getProxyPendingActivity(BActivityThread.getAppPid())));
                        ProxyPendingRecord.saveStub(shadow, intent, BActivityThread.getUserId());
                        intents[i] = shadow;
                        break;
                }'''
    new = '''                switch (type) {
                    case ActivityManagerCompat.INTENT_SENDER_ACTIVITY: {
                        Intent shadow = pendingShadowFor(intent);
                        shadow.setComponent(new ComponentName(
                                BlackBoxCore.getHostPkg(),
                                ProxyManifest.getProxyPendingActivity(BActivityThread.getAppPid())));
                        sanitizePendingTarget(intent);
                        ProxyPendingRecord.saveStub(shadow, intent, BActivityThread.getUserId());
                        intents[i] = shadow;
                        break;
                    }
                    case ActivityManagerCompat.INTENT_SENDER_BROADCAST: {
                        Intent shadow = pendingShadowFor(intent);
                        shadow.setComponent(new ComponentName(
                                BlackBoxCore.getHostPkg(),
                                "top.niunaijun.blackbox.proxy.ProxyPendingReceiver"));
                        sanitizePendingTarget(intent);
                        ProxyPendingRecord.saveStub(shadow, intent, BActivityThread.getUserId());
                        intents[i] = shadow;
                        break;
                    }
                    case ActivityManagerCompat.INTENT_SENDER_SERVICE:
                    case 5: { // ActivityManager.INTENT_SENDER_FOREGROUND_SERVICE on O+
                        Intent shadow = pendingShadowFor(intent);
                        shadow.setComponent(new ComponentName(
                                BlackBoxCore.getHostPkg(),
                                "top.niunaijun.blackbox.proxy.ProxyPendingService"));
                        if (type == 5) {
                            shadow.putExtra(
                                    "_B_|_P_require_foreground_", true);
                        }
                        sanitizePendingTarget(intent);
                        ProxyPendingRecord.saveStub(shadow, intent, BActivityThread.getUserId());
                        intents[i] = shadow;
                        break;
                    }
                }'''
    text = replace_once(text, old, new, "translate broadcast/service PendingIntents")

    marker = '''        private int getIntentsIndex(Object[] args) {'''
    helpers = r'''        /**
         * PendingIntent identity ignores extras, so preserve the original intent's
         * filter fields on the real helper intent while replacing only its component.
         * This avoids collisions between unrelated legacy alarms/reminders sharing a
         * requestCode, without exposing the virtual component to system_server.
         */
        private Intent pendingShadowFor(Intent target) {
            Intent shadow = new Intent();
            if (target == null) return shadow;
            shadow.setAction(target.getAction());
            try {
                shadow.setDataAndType(target.getData(), target.getType());
            } catch (Throwable ignored) {
                shadow.setData(target.getData());
            }
            if (target.getCategories() != null) {
                for (String category : target.getCategories()) shadow.addCategory(category);
            }
            return shadow;
        }

        private void sanitizePendingTarget(Intent target) {
            try {
                IntentSanitizer.sanitizeClassExtrasForIpc(target);
            } catch (Throwable ignored) {
            }
        }

'''
    if "private Intent pendingShadowFor(Intent target)" not in text:
        if marker not in text:
            raise SystemExit("[universal-runtime] pending helper insertion point missing")
        text = text.replace(marker, helpers + marker, 1)
        print("[universal-runtime] PendingIntent filter identity + IPC sanitizer: applied")

    path.write_text(text, encoding="utf-8")


def patch_manifest(root: Path) -> None:
    path = root / "Bcore/src/main/AndroidManifest.xml"
    text = path.read_text(encoding="utf-8")
    marker = '''        <receiver
            android:name=".proxy.ProxyBroadcastReceiver"
            android:enabled="true"
            android:exported="true"
            android:process="@string/black_box_service_name">
            <intent-filter>
                <action android:name="${applicationId}.stub_receiver" />
            </intent-filter>
        </receiver>
'''
    insert = marker + '''
        <!-- Stable real endpoints for guest PendingIntent broadcast/service tokens. -->
        <receiver
            android:name=".proxy.ProxyPendingReceiver"
            android:enabled="true"
            android:exported="false" />

        <service
            android:name=".proxy.ProxyPendingService"
            android:enabled="true"
            android:exported="false" />

        <!-- A virtual manifest receiver cannot wake a package Android cannot see.
             Wake the helper after boot, then relay BOOT_COMPLETED into the virtual PM. -->
        <receiver
            android:name=".proxy.LegacyBootReceiver"
            android:enabled="true"
            android:exported="true"
            android:process="@string/black_box_service_name">
            <intent-filter>
                <action android:name="android.intent.action.BOOT_COMPLETED" />
            </intent-filter>
        </receiver>
'''
    text = replace_once(text, marker, insert, "declare PendingIntent + boot bridge components")
    path.write_text(text, encoding="utf-8")


def write_boot_receiver(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/proxy/LegacyBootReceiver.java"
    source = r'''package top.niunaijun.blackbox.proxy;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.RemoteException;

import top.niunaijun.blackbox.BlackBoxCore;
import top.niunaijun.blackbox.entity.am.PendingResultData;
import top.niunaijun.blackbox.utils.Slog;

/** Wakes the virtual broadcast graph after reboot and relays BOOT_COMPLETED. */
public class LegacyBootReceiver extends BroadcastReceiver {
    private static final String TAG = "LegacyBootReceiver";

    @Override
    public void onReceive(Context context, Intent systemIntent) {
        if (systemIntent == null
                || !Intent.ACTION_BOOT_COMPLETED.equals(systemIntent.getAction())) {
            return;
        }
        PendingResult pending = goAsync();
        try {
            Intent guestIntent = new Intent(Intent.ACTION_BOOT_COMPLETED);
            BlackBoxCore.getBActivityManager().scheduleBroadcastReceiver(
                    guestIntent, new PendingResultData(pending), 0);
        } catch (RemoteException e) {
            pending.finish();
        } catch (Throwable t) {
            Slog.e(TAG, "Could not relay BOOT_COMPLETED to virtual packages", t);
            pending.finish();
        }
    }
}
'''
    path.write_text(source, encoding="utf-8")
    print("[universal-runtime] BOOT_COMPLETED relay: written")


def patch_cold_receivers(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/core/system/am/BActivityManagerService.java"
    text = path.read_text(encoding="utf-8")
    old = '''            ProcessRecord processRecord = BProcessManagerService.get().findProcessRecord(
                    resolve.activityInfo.packageName,
                    resolve.activityInfo.processName,
                    userId
            );
            if (processRecord != null) {
                ReceiverData data = new ReceiverData();
                data.intent = intent;
                data.activityInfo = resolve.activityInfo;
                data.data = pendingResultData;
                processRecord.bActivityThread.scheduleReceiver(data);
            }'''
    new = '''            ProcessRecord processRecord = BProcessManagerService.get().findProcessRecord(
                    resolve.activityInfo.packageName,
                    resolve.activityInfo.processName,
                    userId
            );
            if (processRecord == null) {
                // Android normally cold-starts an application for a manifest receiver.
                // The upstream sandbox only delivered to an already-running process,
                // silently losing boot/alarm/connectivity broadcasts for stopped guests.
                processRecord = BProcessManagerService.get().startProcessLocked(
                        resolve.activityInfo.packageName,
                        resolve.activityInfo.processName,
                        userId,
                        -1,
                        Binder.getCallingPid());
            }
            if (processRecord != null) {
                try {
                    processRecord.bActivityThread.bindApplication();
                } catch (RemoteException ignored) {
                }
                ReceiverData data = new ReceiverData();
                data.intent = intent;
                data.activityInfo = resolve.activityInfo;
                data.data = pendingResultData;
                processRecord.bActivityThread.scheduleReceiver(data);
            }'''
    text = replace_once(text, old, new, "cold-start manifest receiver process")
    path.write_text(text, encoding="utf-8")


def patch_broadcast_registry(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/core/system/am/BroadcastManager.java"
    text = path.read_text(encoding="utf-8")

    old_registration = '''                    ProxyBroadcastReceiver proxyBroadcastReceiver = new ProxyBroadcastReceiver();
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        BlackBoxCore.getContext().registerReceiver(proxyBroadcastReceiver, intent.intentFilter, Context.RECEIVER_EXPORTED);
                    }else{
                        BlackBoxCore.getContext().registerReceiver(proxyBroadcastReceiver, intent.intentFilter);
                    }
                    addReceiver(bPackage.packageName, proxyBroadcastReceiver);'''
    new_registration = '''                    ProxyBroadcastReceiver proxyBroadcastReceiver = new ProxyBroadcastReceiver();
                    try {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            BlackBoxCore.getContext().registerReceiver(
                                    proxyBroadcastReceiver,
                                    intent.intentFilter,
                                    Context.RECEIVER_EXPORTED);
                        } else {
                            BlackBoxCore.getContext().registerReceiver(
                                    proxyBroadcastReceiver, intent.intentFilter);
                        }
                        addReceiver(bPackage.packageName, proxyBroadcastReceiver);
                    } catch (Throwable registrationFailure) {
                        // Protected/OEM-only actions may reject third-party dynamic
                        // registration. One such filter must not prevent every other
                        // manifest receiver in the same legacy package from working.
                        Slog.w(TAG, "Skipping unsupported receiver filter for "
                                + bPackage.packageName + ": " + registrationFailure.getMessage());
                    }'''
    text = replace_once(text, old_registration, new_registration, "isolate receiver filter registration failures")

    old_uninstall = '''        if (removeApp) {
            synchronized (mReceivers) {
                List<BroadcastReceiver> broadcastReceivers = mReceivers.get(packageName);
                if (broadcastReceivers != null) {
                    Slog.d(TAG, "unregisterReceiver Package: " + packageName + ", size: " + broadcastReceivers.size());
                    for (BroadcastReceiver broadcastReceiver : broadcastReceivers) {
                        try {
                            BlackBoxCore.getContext().unregisterReceiver(broadcastReceiver);
                        } catch (Throwable ignored) {
                        }
                    }
                }
                mReceivers.remove(packageName);
            }
        }'''
    new_uninstall = '''        if (removeApp) {
            synchronized (mReceivers) {
                unregisterPackageLocked(packageName);
            }
        }'''
    text = replace_once(text, old_uninstall, new_uninstall, "centralize dynamic receiver cleanup")

    old_install = '''        synchronized (mReceivers) {
            mReceivers.remove(packageName);
            BPackageSettings bPackageSetting = mPms.getBPackageSetting(packageName);
            if (bPackageSetting != null) {
                registerPackage(bPackageSetting.pkg);
            }
        }'''
    new_install = '''        synchronized (mReceivers) {
            // Reinstall/update must unregister the previous real receiver objects;
            // simply removing the bookkeeping list leaves duplicate live callbacks.
            unregisterPackageLocked(packageName);
            BPackageSettings bPackageSetting = mPms.getBPackageSetting(packageName);
            if (bPackageSetting != null) {
                registerPackage(bPackageSetting.pkg);
            }
        }'''
    text = replace_once(text, old_install, new_install, "prevent duplicate receivers after reinstall")

    marker = '''    @Override
    public void onPackageUninstalled(String packageName, boolean removeApp, int userId) {'''
    helper = '''    private void unregisterPackageLocked(String packageName) {
        List<BroadcastReceiver> broadcastReceivers = mReceivers.remove(packageName);
        if (broadcastReceivers == null) return;
        Slog.d(TAG, "unregisterReceiver Package: " + packageName
                + ", size: " + broadcastReceivers.size());
        for (BroadcastReceiver broadcastReceiver : broadcastReceivers) {
            try {
                BlackBoxCore.getContext().unregisterReceiver(broadcastReceiver);
            } catch (Throwable ignored) {
            }
        }
    }

'''
    if "private void unregisterPackageLocked(String packageName)" not in text:
        if marker not in text:
            raise SystemExit("[universal-runtime] receiver cleanup insertion point missing")
        text = text.replace(marker, helper + marker, 1)
        print("[universal-runtime] receiver cleanup helper: applied")

    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    alarm = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IAlarmManagerProxy.java").read_text(encoding="utf-8")
    am = (root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IActivityManagerProxy.java").read_text(encoding="utf-8")
    ams = (root / "Bcore/src/main/java/top/niunaijun/blackbox/core/system/am/BActivityManagerService.java").read_text(encoding="utf-8")
    broadcasts = (root / "Bcore/src/main/java/top/niunaijun/blackbox/core/system/am/BroadcastManager.java").read_text(encoding="utf-8")
    manifest = (root / "Bcore/src/main/AndroidManifest.xml").read_text(encoding="utf-8")

    required = {
        "alarm pass-through": (alarm, "return method.invoke(who, args);"),
        "alarm package translation": (alarm, "MethodParameterUtils.replaceAllAppPkg(args);"),
        "pending broadcast": (am, "ProxyPendingReceiver"),
        "pending service": (am, "ProxyPendingService"),
        "pending sanitizer": (am, "sanitizePendingTarget(intent)"),
        "cold receiver": (ams, "startProcessLocked("),
        "registration isolation": (broadcasts, "Skipping unsupported receiver filter"),
        "reinstall cleanup": (broadcasts, "unregisterPackageLocked(packageName)"),
        "boot receiver manifest": (manifest, ".proxy.LegacyBootReceiver"),
        "pending receiver manifest": (manifest, ".proxy.ProxyPendingReceiver"),
        "pending service manifest": (manifest, ".proxy.ProxyPendingService"),
    }
    for label, (text, needle) in required.items():
        if needle not in text:
            raise SystemExit(f"[universal-runtime] verification failed: {label}")

    if '''protected Object hook(Object who, Method method, Object[] args) throws Throwable {\n            return 0;\n        }''' in alarm:
        raise SystemExit("[universal-runtime] alarm no-op still present")

    for file_name in ("ProxyPendingReceiver.java", "ProxyPendingService.java", "LegacyBootReceiver.java"):
        if not (root / "Bcore/src/main/java/top/niunaijun/blackbox/proxy" / file_name).is_file():
            raise SystemExit(f"[universal-runtime] missing generated proxy: {file_name}")

    print("[universal-runtime] alarms + PendingIntents + cold receivers + boot relay verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_universal_legacy_runtime.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    patch_alarm_manager(root)
    write_pending_components(root)
    patch_pending_intents(root)
    patch_manifest(root)
    write_boot_receiver(root)
    patch_cold_receivers(root)
    patch_broadcast_registry(root)
    verify(root)


if __name__ == "__main__":
    main()
