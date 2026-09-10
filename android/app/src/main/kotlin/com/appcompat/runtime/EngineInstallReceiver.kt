package com.appcompat.runtime

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import android.util.Log

object EngineInstallCoordinator {
    @Volatile
    var callback: ((bits: Int, success: Boolean, message: String?) -> Unit)? = null

    fun complete(bits: Int, success: Boolean, message: String?) {
        val listener = callback
        if (success || listener != null) {
            listener?.invoke(bits, success, message)
        }
        if (success || !success) callback = null
    }
}

class EngineInstallReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val bits = intent.getIntExtra("engineBits", 0)
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        val message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)

        when (status) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirmIntent = if (Build.VERSION.SDK_INT >= 33) {
                    intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                } else {
                    @Suppress("DEPRECATION")
                    intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                }
                if (confirmIntent != null) {
                    confirmIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    context.startActivity(confirmIntent)
                } else {
                    EngineInstallCoordinator.complete(bits, false, "Android did not provide an engine installation confirmation screen.")
                }
            }
            PackageInstaller.STATUS_SUCCESS -> {
                EngineInstallCoordinator.complete(bits, true, null)
            }
            else -> {
                Log.e("AppCompat", "Engine installation failed: $status $message")
                EngineInstallCoordinator.complete(bits, false, message ?: "Compatibility engine installation failed.")
            }
        }
    }
}
