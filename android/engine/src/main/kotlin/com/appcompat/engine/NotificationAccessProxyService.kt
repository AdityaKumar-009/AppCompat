package com.appcompat.engine

import android.content.ComponentName
import android.content.Intent
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import top.niunaijun.blackbox.BlackBoxCore
import top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors

/**
 * Android can only grant notification-listener access to a real installed service.
 * Virtual guest services are invisible to Settings, so this real helper service owns
 * the system grant and forwards callbacks into explicitly opted-in virtual listeners.
 *
 * Do not instantiate a guest listener merely because the user toggled the Android
 * permission. Many old apps use that grant as part of a multi-step onboarding flow
 * and their listener onCreate() immediately starts large background/overlay services.
 * Starting those services while Settings is still returning to the tutorial creates
 * a race and can crash the whole virtual process. The permission state is exposed to
 * legacy getRunningServices() callers by the engine; the guest listener itself is
 * materialized lazily when a real notification callback actually needs it.
 */
class NotificationAccessProxyService : NotificationListenerService() {
    companion object {
        private const val TAG = "AppCompatSpecialAccess"
        private const val USER_ID = 0
    }

    private val dispatcher = Executors.newSingleThreadExecutor()
    private val forwardedGuests = ConcurrentHashMap.newKeySet<String>()

    override fun onListenerConnected() {
        super.onListenerConnected()
        // Intentionally no eager guest start and no active-notification replay here.
        // AppServiceDispatcher guarantees onListenerConnected() is delivered before
        // the first posted/removed callback when a guest is lazily materialized.
        Log.i(TAG, "Real notification listener connected; guest listeners remain lazy")
    }

    override fun onListenerDisconnected() {
        // Only disconnect virtual listeners that this proxy actually materialized.
        // Otherwise a system-side disconnect would paradoxically start a guest just
        // to tell it that it is disconnected.
        dispatchDisconnectedToMaterializedGuests()
        super.onListenerDisconnected()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn != null) {
            dispatch(LegacySpecialAccessCompat.ACTION_NOTIFICATION_POSTED, sbn)
        }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {
        if (sbn != null) {
            dispatch(LegacySpecialAccessCompat.ACTION_NOTIFICATION_REMOVED, sbn)
        }
    }

    private fun dispatch(action: String, sbn: StatusBarNotification?) {
        dispatcher.execute {
            val components = guestComponents()
            for (component in components) {
                try {
                    forwardedGuests.add(component.flattenToString())
                    startGuestBridge(component, action, sbn)
                } catch (t: Throwable) {
                    Log.w(TAG, "Could not forward notification callback to $component", t)
                }
            }
        }
    }

    private fun dispatchDisconnectedToMaterializedGuests() {
        dispatcher.execute {
            val components = guestComponents()
            for (component in components) {
                val key = component.flattenToString()
                if (!forwardedGuests.remove(key)) continue
                try {
                    startGuestBridge(
                        component,
                        LegacySpecialAccessCompat.ACTION_NOTIFICATION_LISTENER_DISCONNECTED,
                        null,
                    )
                } catch (t: Throwable) {
                    Log.w(TAG, "Could not disconnect virtual notification listener $component", t)
                }
            }
            forwardedGuests.clear()
        }
    }

    private fun guestComponents(): List<ComponentName> = try {
        LegacySpecialAccessCompat.notificationListenerGuestComponents()
    } catch (t: Throwable) {
        Log.w(TAG, "Unable to enumerate opted-in guest notification listeners", t)
        emptyList()
    }

    private fun startGuestBridge(
        component: ComponentName,
        action: String,
        sbn: StatusBarNotification?,
    ) {
        val bridgeIntent = Intent(action).apply {
            this.component = component
            if (sbn != null) {
                putExtra(LegacySpecialAccessCompat.EXTRA_STATUS_BAR_NOTIFICATION, sbn)
            }
        }
        BlackBoxCore.getBActivityManager().startService(
            bridgeIntent,
            null,
            false,
            USER_ID,
        )
    }

    override fun onDestroy() {
        dispatcher.shutdownNow()
        forwardedGuests.clear()
        super.onDestroy()
    }
}
