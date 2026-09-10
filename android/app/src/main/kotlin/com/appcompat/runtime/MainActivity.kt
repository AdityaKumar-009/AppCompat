package com.appcompat.runtime

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Process
import android.util.Log
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.Executors

class MainActivity : FlutterActivity() {
    companion object {
        private const val CHANNEL = "com.appcompat.runtime/bridge"
        private const val REQUEST_APK = 4401
        private const val PREFS = "appcompat_runtime"
        private const val PENDING_NATIVE_PACKAGE = "pending_native_package"
        private const val PENDING_NATIVE_STARTED_AT = "pending_native_started_at"
    }

    private val worker = Executors.newSingleThreadExecutor()
    private var pendingPickerResult: MethodChannel.Result? = null
    private var lastReport: Map<String, Any?>? = null
    private var resumeCount = 0

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler(::handleCall)
    }

    private fun handleCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "selectAndAnalyze" -> selectAndAnalyze(result)
            "engineStatus" -> result.success(
                mapOf(
                    "ready" to CompatRuntime.ready,
                    "hostSdk" to android.os.Build.VERSION.SDK_INT,
                    "hostAbis" to android.os.Build.SUPPORTED_ABIS.toList(),
                    "runtimeBits" to if (Process.is64Bit()) 64 else 32,
                    "smartRouting" to true
                )
            )
            "runVirtual" -> runSmart(call, result)
            "listVirtualApps" -> background(result) { CompatRuntime.listApps() }
            "launchVirtual" -> {
                val packageName = call.argument<String>("packageName")
                if (packageName.isNullOrBlank()) {
                    result.error("bad_arguments", "Package name is missing.", null)
                } else {
                    background(result) { CompatRuntime.launch(packageName) }
                }
            }
            "removeVirtual" -> {
                val packageName = call.argument<String>("packageName")
                if (packageName.isNullOrBlank()) {
                    result.error("bad_arguments", "Package name is missing.", null)
                } else {
                    background(result) {
                        CompatRuntime.remove(packageName)
                        true
                    }
                }
            }
            "installNormally" -> installNormally(call, result)
            else -> result.notImplemented()
        }
    }

    /**
     * The Flutter UI historically calls this operation "runVirtual". It is now a
     * smart router: if Android can safely execute the APK itself, native execution
     * wins. The virtual engine is only used when the platform's target-SDK floor
     * blocks normal installation and the guest ABI matches this process.
     */
    private fun runSmart(call: MethodCall, result: MethodChannel.Result) {
        val path = call.argument<String>("path")
        val packageName = call.argument<String>("packageName")
        if (path.isNullOrBlank() || packageName.isNullOrBlank()) {
            result.error("bad_arguments", "APK path or package name is missing.", null)
            return
        }

        val report = lastReport
        val isCurrentReport = report?.get("packageName") == packageName && report["path"] == path
        val route = if (isCurrentReport) report?.get("route")?.toString() else null

        if (route == "native") {
            val uri = report?.get("uri")?.toString().orEmpty()
            if (uri.isBlank()) {
                result.error("native_uri_missing", "The original APK permission is no longer available. Choose the APK again.", null)
                return
            }
            try {
                openNativeInstaller(uri, packageName)
                result.success(
                    mapOf(
                        "launched" to false,
                        "nativeInstallStarted" to true,
                        "packageName" to packageName,
                        "message" to "Android's native installer was opened because it is the more compatible path for this APK. After installation, AppCompat will open the app automatically."
                    )
                )
            } catch (t: Throwable) {
                result.error("installer_unavailable", "Android could not open its package installer.", t.message)
            }
            return
        }

        if (route == "limited") {
            val message = (report?.get("issues") as? List<*>)
                ?.lastOrNull()
                ?.toString()
                ?: "This APK needs a CPU/runtime capability that this device cannot provide safely."
            result.success(mapOf("launched" to false, "message" to message, "packageName" to packageName))
            return
        }

        background(result) { CompatRuntime.installAndLaunch(path, packageName) }
    }

    private fun selectAndAnalyze(result: MethodChannel.Result) {
        if (pendingPickerResult != null) {
            result.error("picker_busy", "The APK picker is already open.", null)
            return
        }
        pendingPickerResult = result
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "application/vnd.android.package-archive"
            putExtra(
                Intent.EXTRA_MIME_TYPES,
                arrayOf("application/vnd.android.package-archive", "application/octet-stream")
            )
        }
        try {
            startActivityForResult(intent, REQUEST_APK)
        } catch (t: Throwable) {
            pendingPickerResult = null
            result.error("picker_unavailable", "No file picker is available on this device.", t.message)
        }
    }

    @Deprecated("Deprecated in Android framework; retained for broad device compatibility")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_APK) return

        val result = pendingPickerResult ?: return
        pendingPickerResult = null
        val uri = data?.data
        if (resultCode != Activity.RESULT_OK || uri == null) {
            result.success(null)
            return
        }

        try {
            val flags = data.flags and
                (Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            contentResolver.takePersistableUriPermission(uri, flags and Intent.FLAG_GRANT_READ_URI_PERMISSION)
        } catch (_: Throwable) {
            // Some providers grant only a transient URI. The analyzer immediately makes a private copy.
        }

        worker.execute {
            try {
                val report = ApkAnalyzer.analyze(applicationContext, uri)
                lastReport = report
                runOnUiThread { result.success(report) }
            } catch (t: Throwable) {
                Log.e("AppCompat", "APK analysis failed", t)
                runOnUiThread {
                    result.error("analysis_failed", t.message ?: "Could not analyze this APK.", null)
                }
            }
        }
    }

    private fun installNormally(call: MethodCall, result: MethodChannel.Result) {
        val rawUri = call.argument<String>("uri")
        if (rawUri.isNullOrBlank()) {
            result.error("bad_arguments", "Original APK URI is missing.", null)
            return
        }
        val packageName = lastReport?.get("packageName")?.toString().orEmpty()
        try {
            openNativeInstaller(rawUri, packageName.ifBlank { null })
            result.success(null)
        } catch (t: Throwable) {
            result.error("installer_unavailable", "Android could not open its package installer.", t.message)
        }
    }

    private fun openNativeInstaller(rawUri: String, packageName: String?) {
        if (!packageName.isNullOrBlank()) {
            getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putString(PENDING_NATIVE_PACKAGE, packageName)
                .putLong(PENDING_NATIVE_STARTED_AT, System.currentTimeMillis())
                .apply()
        }

        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(Uri.parse(rawUri), "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(intent)
    }

    override fun onResume() {
        super.onResume()
        resumeCount++
        // Do not do this on the first resume after Activity creation. On a later
        // resume, we have just returned from Android's package installer.
        if (resumeCount > 1) maybeOpenNewlyInstalledApp()
    }

    private fun maybeOpenNewlyInstalledApp() {
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        val packageName = prefs.getString(PENDING_NATIVE_PACKAGE, null) ?: return
        val startedAt = prefs.getLong(PENDING_NATIVE_STARTED_AT, 0L)

        val installed = try {
            if (android.os.Build.VERSION.SDK_INT >= 33) {
                packageManager.getPackageInfo(packageName, PackageManager.PackageInfoFlags.of(0))
            } else {
                @Suppress("DEPRECATION")
                packageManager.getPackageInfo(packageName, 0)
            }
            true
        } catch (_: Throwable) {
            false
        }

        if (!installed) {
            // Returning from the installer without a package means the user cancelled
            // or Android rejected it. Clear the pending state so AppCompat never loops.
            if (System.currentTimeMillis() - startedAt > 500L) {
                prefs.edit().remove(PENDING_NATIVE_PACKAGE).remove(PENDING_NATIVE_STARTED_AT).apply()
            }
            return
        }

        prefs.edit().remove(PENDING_NATIVE_PACKAGE).remove(PENDING_NATIVE_STARTED_AT).apply()
        try {
            val launchIntent = packageManager.getLaunchIntentForPackage(packageName)
                ?: findMainActivity(packageName, Intent.CATEGORY_LAUNCHER)
                ?: findMainActivity(packageName, Intent.CATEGORY_HOME)
            if (launchIntent != null) {
                launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                startActivity(launchIntent)
            } else {
                Log.w("AppCompat", "Installed $packageName but no launchable activity was found")
            }
        } catch (t: Throwable) {
            Log.w("AppCompat", "Could not auto-open $packageName after installation", t)
        }
    }

    private fun findMainActivity(packageName: String, category: String): Intent? {
        val query = Intent(Intent.ACTION_MAIN).apply {
            addCategory(category)
            setPackage(packageName)
        }
        val matches = if (android.os.Build.VERSION.SDK_INT >= 33) {
            packageManager.queryIntentActivities(query, PackageManager.ResolveInfoFlags.of(0))
        } else {
            @Suppress("DEPRECATION")
            packageManager.queryIntentActivities(query, 0)
        }
        val component = matches.firstOrNull()?.activityInfo?.let {
            android.content.ComponentName(it.packageName, it.name)
        } ?: return null
        return Intent(Intent.ACTION_MAIN).apply {
            addCategory(category)
            this.component = component
        }
    }

    private fun background(result: MethodChannel.Result, block: () -> Any?) {
        worker.execute {
            try {
                val value = block()
                runOnUiThread { result.success(value) }
            } catch (t: Throwable) {
                Log.e("AppCompat", "Native operation failed", t)
                runOnUiThread {
                    result.error("native_failure", t.message ?: t.javaClass.simpleName, null)
                }
            }
        }
    }

    override fun onDestroy() {
        pendingPickerResult = null
        super.onDestroy()
    }
}
