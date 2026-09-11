package com.appcompat.engine

import android.app.admin.DeviceAdminReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import top.niunaijun.blackbox.BlackBoxCore
import top.niunaijun.blackbox.utils.compat.LegacySystemComponentCompat

/**
 * Real DeviceAdminReceiver used as the Android-visible principal for legacy guests.
 * User consent still happens in Android's own Device Administrator UI. The virtual
 * component is restored when callbacks and DevicePolicyManager queries return to the
 * guest runtime.
 */
class LegacyDeviceAdminReceiver : DeviceAdminReceiver() {
    companion object {
        private const val TAG = "AppCompatDeviceAdmin"
        private const val USER_ID = 0
    }

    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        val action = intent.action ?: return
        val guestComponents = try {
            LegacySystemComponentCompat.deviceAdminGuestComponents()
        } catch (t: Throwable) {
            Log.w(TAG, "Unable to enumerate guest device-admin receivers", t)
            emptyList()
        }

        for (component in guestComponents) {
            try {
                val guestIntent = Intent(intent).apply {
                    this.component = component
                    setPackage(component.packageName)
                }
                BlackBoxCore.getBActivityManager().sendBroadcast(guestIntent, null, USER_ID)
            } catch (t: Throwable) {
                Log.w(TAG, "Could not forward device-admin callback to $component for $action", t)
            }
        }
    }
}
