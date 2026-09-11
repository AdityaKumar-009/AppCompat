package com.appcompat.engine

import android.content.Intent
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import top.niunaijun.blackbox.BlackBoxCore
import top.niunaijun.blackbox.utils.compat.LegacySpecialAccessCompat
import java.util.concurrent.Executors

/**
 * Android can only grant notification-listener access to a real installed service.
 * Virtual guest services are invisible to Settings, so this real helper service owns
 * the system grant and forwards callbacks into each explicitly opted-in virtual
 * NotificationListenerService.
 */
class NotificationAccessProxyService : NotificationListenerService() {
    companion object {
        private const val TAG = "AppCompatSpecialAccess"
        private const val USER_ID = 0
    }

    // Android N+ delivers NotificationListenerService callbacks on the main thread.
    // Keep virtual-package IPC ordered but off that thread so a slow legacy guest
    // cannot make SystemUI consider the real listener unresponsive.
    private val dispatcher = Executors.newSingleThreadExecutor()

    override fun onListenerConnected() {
        super.onListenerConnected()
        dispatch(LegacySpecialAccessCompat.ACTION_NOTIFICATION_LISTENER_CONNECTED, null)

        // Legacy listeners commonly call getActiveNotifications() from their own
        // onListenerConnected(). Their virtual service is not the Binder endpoint
        // Android registered, so proactively replay the real proxy's current set.
        // This also gives apps installed after notifications were already present a
        // coherent initial state rather than waiting for the next notification.
        val existing = try {
            activeNotifications?.toList().orEmpty()
        } catch (t: Throwable) {
            Log.w(TAG, "Unable to read active notifications for initial replay", t)
            emptyList()
        }
        for (notification in existing) {
            dispatch(LegacySpecialAccessCompat.ACTION_NOTIFICATION_POSTED, notification)
        }
    }

    override fun onListenerDisconnected() {
        dispatch(LegacySpecialAccessCompat.ACTION_NOTIFICATION_LISTENER_DISCONNECTED, null)
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
            val components = try {
                LegacySpecialAccessCompat.notificationListenerGuestComponents()
            } catch (t: Throwable) {
                Log.w(TAG, "Unable to enumerate opted-in guest notification listeners", t)
                emptyList()
            }

            for (component in components) {
                try {
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
                } catch (t: Throwable) {
                    Log.w(TAG, "Could not forward notification callback to $component", t)
                }
            }
        }
    }

    override fun onDestroy() {
        dispatcher.shutdownNow()
        super.onDestroy()
    }
}
