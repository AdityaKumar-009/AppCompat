package com.appcompat.engine

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import java.util.concurrent.Executors

class EngineBridgeActivity : Activity() {
    companion object {
        const val ACTION_RUN = "com.appcompat.runtime.engine.RUN"
        const val ACTION_LAUNCH = "com.appcompat.runtime.engine.LAUNCH"
        const val ACTION_REMOVE = "com.appcompat.runtime.engine.REMOVE"
        const val ACTION_LAST_CRASH = "com.appcompat.runtime.engine.LAST_CRASH"
        private const val REQUEST_GUEST_PERMISSIONS = 7011
    }

    private val worker = Executors.newSingleThreadExecutor()
    private var pendingGuestPackage: String = ""
    private var pendingFreshImport = false
    private var pendingInstallResult: Map<String, Any?> = emptyMap()
    private var pendingRequestedPermissions: Array<String> = emptyArray()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val action = intent?.action
        val packageName = intent?.getStringExtra("packageName").orEmpty()

        when (action) {
            ACTION_RUN -> installThenPermissionAwareLaunch(packageName)
            ACTION_LAUNCH -> permissionAwareLaunch(packageName, freshImport = false, installResult = mapOf("installed" to true))
            ACTION_REMOVE -> worker.execute {
                val result = try {
                    if (packageName.isBlank()) {
                        mapOf("removed" to false, "message" to "Package name is missing.")
                    } else {
                        EngineRuntime.remove(packageName)
                    }
                } catch (t: Throwable) {
                    Log.e("AppCompatEngine", "Remove request failed", t)
                    mapOf("removed" to false, "message" to (t.message ?: t.javaClass.simpleName))
                }
                finishWithResult(action, result)
            }
            ACTION_LAST_CRASH -> worker.execute {
                finishWithResult(action, EngineRuntime.lastCrash())
            }
            else -> finishWithResult(
                action,
                mapOf("launched" to false, "message" to "Unknown compatibility engine request.")
            )
        }
    }

    private fun installThenPermissionAwareLaunch(packageName: String) {
        val uri = intent?.data?.toString()
            ?.takeIf { it.isNotBlank() }
            ?: intent?.getStringExtra("uri").orEmpty()
        if (packageName.isBlank() || uri.isBlank()) {
            finishWithResult(
                ACTION_RUN,
                mapOf("launched" to false, "installed" to false, "message" to "APK URI or package name is missing.")
            )
            return
        }

        worker.execute {
            val installResult = try {
                EngineRuntime.install(applicationContext, uri, packageName)
            } catch (t: Throwable) {
                Log.e("AppCompatEngine", "Virtual install failed", t)
                mapOf(
                    "launched" to false,
                    "installed" to false,
                    "packageName" to packageName,
                    "message" to (t.message ?: t.javaClass.simpleName)
                )
            }

            if (installResult["installed"] != true) {
                finishWithResult(ACTION_RUN, installResult)
                return@execute
            }

            val installedPackage = installResult["packageName"]?.toString().orEmpty().ifBlank { packageName }
            runOnUiThread {
                permissionAwareLaunch(installedPackage, freshImport = true, installResult = installResult)
            }
        }
    }

    /**
     * The helper package is the actual Android UID. Before starting the virtual
     * guest, translate its manifest permissions to current Android runtime
     * permissions and let Android show its real consent dialogs for this helper.
     */
    private fun permissionAwareLaunch(
        packageName: String,
        freshImport: Boolean,
        installResult: Map<String, Any?>
    ) {
        if (packageName.isBlank()) {
            finishWithResult(
                intent?.action,
                mapOf("launched" to false, "installed" to false, "message" to "Package name is missing.")
            )
            return
        }

        pendingGuestPackage = packageName
        pendingFreshImport = freshImport
        pendingInstallResult = installResult

        val missing = if (Build.VERSION.SDK_INT >= 23) {
            EngineRuntime.requiredHostRuntimePermissions(packageName)
                .filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
                .distinct()
                .toTypedArray()
        } else {
            emptyArray()
        }

        if (missing.isEmpty()) {
            launchPendingGuest(permissionDeniedCount = 0)
            return
        }

        pendingRequestedPermissions = missing
        try {
            Log.i("AppCompatEngine", "Requesting translated guest permissions for $packageName: ${missing.contentToString()}")
            requestPermissions(missing, REQUEST_GUEST_PERMISSIONS)
        } catch (t: Throwable) {
            // A vendor permission controller can reject a mixed request even when
            // individual permissions are valid. Continue with truthful denied
            // state instead of crashing the guest bootstrap activity.
            Log.w("AppCompatEngine", "Permission preflight could not open", t)
            launchPendingGuest(permissionDeniedCount = missing.size)
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_GUEST_PERMISSIONS) return

        val denied = permissions.indices.count { index ->
            grantResults.getOrNull(index) != PackageManager.PERMISSION_GRANTED
        }
        Log.i(
            "AppCompatEngine",
            "Permission preflight completed for $pendingGuestPackage: requested=${permissions.size}, denied=$denied"
        )
        launchPendingGuest(permissionDeniedCount = denied)
    }

    private fun launchPendingGuest(permissionDeniedCount: Int) {
        val packageName = pendingGuestPackage
        val freshImport = pendingFreshImport
        val installResult = pendingInstallResult
        val requestedCount = pendingRequestedPermissions.size
        pendingRequestedPermissions = emptyArray()

        worker.execute {
            val launchResult = try {
                EngineRuntime.launchInstalled(packageName, freshImport).toMutableMap().apply {
                    put("installed", installResult["installed"] == true || this["installed"] == true)
                    put("permissionsRequested", requestedCount)
                    put("permissionsDenied", permissionDeniedCount)
                    if (permissionDeniedCount > 0 && this["launched"] == true) {
                        put(
                            "message",
                            "Launched with $permissionDeniedCount translated Android permission(s) still denied. The legacy app will see those capabilities as unavailable."
                        )
                    }
                }
            } catch (t: Throwable) {
                Log.e("AppCompatEngine", "Permission-aware guest launch failed", t)
                linkedMapOf<String, Any?>(
                    "launched" to false,
                    "installed" to true,
                    "packageName" to packageName,
                    "permissionsRequested" to requestedCount,
                    "permissionsDenied" to permissionDeniedCount,
                    "message" to (t.message ?: t.javaClass.simpleName)
                )
            }
            finishWithResult(intent?.action, launchResult)
        }
    }

    private fun finishWithResult(action: String?, result: Map<String, Any?>) {
        runOnUiThread {
            if (isFinishing) return@runOnUiThread
            val data = Intent()
            result.forEach { (key, value) ->
                when (value) {
                    is Boolean -> data.putExtra(key, value)
                    is Int -> data.putExtra(key, value)
                    is Long -> data.putExtra(key, value)
                    is String -> data.putExtra(key, value)
                    null -> Unit
                    else -> data.putExtra(key, value.toString())
                }
            }
            val ok = result["launched"] == true || result["removed"] == true || action == ACTION_LAST_CRASH
            setResult(if (ok) RESULT_OK else RESULT_CANCELED, data)
            finish()
        }
    }

    override fun onDestroy() {
        worker.shutdownNow()
        super.onDestroy()
    }
}
