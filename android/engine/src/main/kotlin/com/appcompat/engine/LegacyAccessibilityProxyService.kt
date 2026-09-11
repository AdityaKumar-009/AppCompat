package com.appcompat.engine

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import top.niunaijun.blackbox.BlackBoxCore
import top.niunaijun.blackbox.utils.compat.LegacySystemComponentCompat
import java.util.concurrent.Executors

/**
 * Real Android AccessibilityService owned by the helper package.
 *
 * A virtual guest service cannot be enumerated or bound by Android Settings. The
 * user therefore enables this real proxy, and AppCompat forwards callbacks only to
 * guest accessibility services whose onboarding explicitly requested access.
 */
class LegacyAccessibilityProxyService : AccessibilityService() {
    companion object {
        private const val TAG = "AppCompatAccessibility"
        private const val USER_ID = 0
    }

    private val dispatcher = Executors.newSingleThreadExecutor()

    override fun onServiceConnected() {
        super.onServiceConnected()
        dispatch(LegacySystemComponentCompat.ACTION_ACCESSIBILITY_CONNECTED, null)
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        val copy = event?.let { AccessibilityEvent.obtain(it) }
        dispatch(LegacySystemComponentCompat.ACTION_ACCESSIBILITY_EVENT, copy)
    }

    override fun onInterrupt() {
        dispatch(LegacySystemComponentCompat.ACTION_ACCESSIBILITY_INTERRUPT, null)
    }

    private fun dispatch(action: String, event: AccessibilityEvent?) {
        dispatcher.execute {
            try {
                val components = LegacySystemComponentCompat.accessibilityGuestComponents()
                for (component in components) {
                    try {
                        val bridge = Intent(action).apply {
                            this.component = component
                            if (event != null) {
                                putExtra(LegacySystemComponentCompat.EXTRA_ACCESSIBILITY_EVENT, event)
                            }
                        }
                        BlackBoxCore.getBActivityManager().startService(
                            bridge,
                            null,
                            false,
                            USER_ID,
                        )
                    } catch (t: Throwable) {
                        Log.w(TAG, "Could not forward accessibility callback to $component", t)
                    }
                }
            } catch (t: Throwable) {
                Log.w(TAG, "Accessibility callback bridge failed", t)
            } finally {
                event?.recycle()
            }
        }
    }

    override fun onDestroy() {
        dispatcher.shutdownNow()
        super.onDestroy()
    }
}
