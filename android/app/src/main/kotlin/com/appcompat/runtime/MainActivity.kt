package com.appcompat.runtime

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.util.Log
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.util.concurrent.Executors

class MainActivity : FlutterActivity() {
    companion object {
        private const val CHANNEL = "com.appcompat.runtime/bridge"
        private const val REQUEST_APK = 4401
        private const val REQUEST_ENGINE = 4402
        private const val REQUEST_UNKNOWN_SOURCES = 4403
        private const val PREFS = "appcompat_runtime"
        private const val PENDING_NATIVE_PACKAGE = "pending_native_package"
        private const val PENDING_NATIVE_STARTED_AT = "pending_native_started_at"
    }

    private enum class EngineOperation { RUN, LIBRARY_RUN, LAUNCH, REMOVE }

    private val worker = Executors.newSingleThreadExecutor()
    private var pendingPickerResult: MethodChannel.Result? = null
    private var pendingEngineResult: MethodChannel.Result? = null
    private var pendingEngineOperation: EngineOperation? = null
    private var pendingEngineBits: Int = 0
    private var pendingEnginePackage: String = ""
    private var pendingEngineName: String = ""
    private var pendingEngineApk: File? = null
    private var waitingForUnknownSources = false
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
            "engineStatus" -> {
                val status = EngineBroker.status(this).toMutableMap()
                status["ready"] = true
                status["hostSdk"] = Build.VERSION.SDK_INT
                status["hostAbis"] = Build.SUPPORTED_ABIS.toList()
                status["smartRouting"] = true
                result.success(status)
            }
            "runVirtual" -> runSmart(call, result)
            "listVirtualApps" -> result.success(EngineBroker.listRegistered(this))
            "launchVirtual" -> {
                val packageName = call.argument<String>("packageName")
                if (packageName.isNullOrBlank()) {
                    result.error("bad_arguments", "Package name is missing.", null)
                } else {
                    val bits = EngineBroker.registeredBits(this, packageName)
                    if (bits == null) {
                        result.success(false)
                    } else {
                        // A helper runtime is versioned and may be replaced between
                        // AppCompat releases. Re-import a retained source APK into
                        // the current helper, while preserving the Boolean result
                        // contract expected by the library's Run button.
                        val sourceApk = EngineBroker.registeredApkPath(this, packageName)
                            ?.let(::File)
                            ?.takeIf { it.isFile && it.length() > 0L }
                        beginEngineOperation(
                            result = result,
                            operation = if (sourceApk != null) EngineOperation.LIBRARY_RUN else EngineOperation.LAUNCH,
                            bits = bits,
                            packageName = packageName,
                            appName = EngineBroker.registeredName(this, packageName),
                            apk = sourceApk
                        )
                    }
                }
            }
            "removeVirtual" -> {
                val packageName = call.argument<String>("packageName")
                if (packageName.isNullOrBlank()) {
                    result.error("bad_arguments", "Package name is missing.", null)
                } else {
                    val bits = EngineBroker.registeredBits(this, packageName)
                    if (bits == null) {
                        EngineBroker.unregister(this, packageName)
                        result.success(null)
                    } else {
                        beginEngineOperation(
                            result = result,
                            operation = EngineOperation.REMOVE,
                            bits = bits,
                            packageName = packageName,
                            appName = EngineBroker.registeredName(this, packageName),
                            apk = null
                        )
                    }
                }
            }
            "installNormally" -> installNormally(call, result)
            else -> result.notImplemented()
        }
    }

    /**
     * The public UI keeps the historical method name `runVirtual`, but this is a
     * progressive router. Real Android execution is used whenever possible; only
     * APKs blocked by modern install policy fall through to an ABI-matched helper.
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
        if (!isCurrentReport) {
            result.error("stale_report", "Choose the APK again so AppCompat can verify its execution route.", null)
            return
        }

        when (report?.get("route")?.toString()) {
            "native" -> {
                val uri = report["uri"]?.toString().orEmpty()
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
                            "message" to "Android's native installer was opened because it preserves more legacy system integration. After installation, AppCompat will try to open the app automatically."
                        )
                    )
                } catch (t: Throwable) {
                    result.error("installer_unavailable", "Android could not open its package installer.", t.message)
                }
            }
            "virtual" -> {
                val bitsFromReport = (report["preferredEngineBits"] as? Number)?.toInt() ?: 0
                val bits = bitsFromReport.takeIf { it == 32 || it == 64 }
                    ?: EngineBroker.chooseBits(report)
                if (bits == null || !EngineBroker.engineSupported(bits)) {
                    result.success(
                        mapOf(
                            "launched" to false,
                            "packageName" to packageName,
                            "message" to "This APK requires a CPU runtime that Android does not expose on this device."
                        )
                    )
                    return
                }
                beginEngineOperation(
                    result = result,
                    operation = EngineOperation.RUN,
                    bits = bits,
                    packageName = packageName,
                    appName = report["name"]?.toString().orEmpty(),
                    apk = File(path)
                )
            }
            else -> {
                val message = (report?.get("issues") as? List<*>)
                    ?.lastOrNull()
                    ?.toString()
                    ?: "This APK needs a platform, CPU or external capability that is unavailable on this device."
                result.success(mapOf("launched" to false, "message" to message, "packageName" to packageName))
            }
        }
    }

    private fun beginEngineOperation(
        result: MethodChannel.Result,
        operation: EngineOperation,
        bits: Int,
        packageName: String,
        appName: String,
        apk: File?
    ) {
        if (pendingEngineResult != null) {
            result.error("engine_busy", "Another compatibility operation is still in progress.", null)
            return
        }
        if (apk != null && (!apk.isFile || apk.length() == 0L)) {
            result.error("apk_missing", "The private APK copy is no longer available. Choose the APK again.", null)
            return
        }

        pendingEngineResult = result
        pendingEngineOperation = operation
        pendingEngineBits = bits
        pendingEnginePackage = packageName
        pendingEngineName = appName
        pendingEngineApk = apk

        if (Build.VERSION.SDK_INT >= 26 && !packageManager.canRequestPackageInstalls() &&
            !EngineBroker.isInstalled(this, bits, pendingEnginePackage)) {
            try {
                waitingForUnknownSources = true
                startActivityForResult(
                    Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${this.packageName}")),
                    REQUEST_UNKNOWN_SOURCES
                )
                return
            } catch (t: Throwable) {
                Log.w("AppCompat", "Could not open unknown-source settings", t)
            }
        }

        provisionAndStartPendingEngine()
    }

    private fun provisionAndStartPendingEngine() {
        val result = pendingEngineResult ?: return
        val bits = pendingEngineBits
        EngineBroker.ensureInstalled(this, bits, pendingEnginePackage) { success, message ->
            runOnUiThread {
                if (pendingEngineResult !== result) return@runOnUiThread
                if (!success) {
                    finishPendingEngineFailure(
                        message ?: "Android did not allow the ${bits}-bit compatibility runtime to be installed."
                    )
                    return@runOnUiThread
                }
                startPendingEngineActivity()
            }
        }
    }

    private fun startPendingEngineActivity() {
        val result = pendingEngineResult ?: return
        val operation = pendingEngineOperation ?: run {
            finishPendingEngineFailure("Compatibility operation state was lost.")
            return
        }
        val action = when (operation) {
            EngineOperation.RUN, EngineOperation.LIBRARY_RUN -> EngineBroker.ACTION_RUN
            EngineOperation.LAUNCH -> EngineBroker.ACTION_LAUNCH
            EngineOperation.REMOVE -> EngineBroker.ACTION_REMOVE
        }

        try {
            EngineBroker.start(
                activity = this,
                bits = pendingEngineBits,
                action = action,
                packageName = pendingEnginePackage,
                apkFile = pendingEngineApk,
                requestCode = REQUEST_ENGINE
            )
        } catch (t: Throwable) {
            Log.e("AppCompat", "Could not start compatibility helper", t)
            if (pendingEngineResult === result) {
                finishPendingEngineFailure(t.message ?: "The compatibility helper could not be started.")
            }
        }
    }

    private fun finishPendingEngineFailure(message: String) {
        val result = pendingEngineResult ?: return
        when (pendingEngineOperation) {
            EngineOperation.LAUNCH, EngineOperation.LIBRARY_RUN -> result.success(false)
            EngineOperation.REMOVE -> result.error("engine_failure", message, null)
            else -> result.success(
                mapOf(
                    "launched" to false,
                    "installed" to false,
                    "packageName" to pendingEnginePackage,
                    "engineBits" to pendingEngineBits,
                    "message" to message
                )
            )
        }
        clearPendingEngine()
    }

    private fun clearPendingEngine() {
        pendingEngineResult = null
        pendingEngineOperation = null
        pendingEngineBits = 0
        pendingEnginePackage = ""
        pendingEngineName = ""
        pendingEngineApk = null
        waitingForUnknownSources = false
    }

    private fun selectAndAnalyze(result: MethodChannel.Result) {
        if (pendingPickerResult != null || pendingEngineResult != null) {
            result.error("busy", "Finish the current compatibility operation first.", null)
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
        when (requestCode) {
            REQUEST_APK -> handleApkPickerResult(resultCode, data)
            REQUEST_ENGINE -> handleEngineResult(resultCode, data)
            REQUEST_UNKNOWN_SOURCES -> {
                waitingForUnknownSources = false
                if (Build.VERSION.SDK_INT < 26 || packageManager.canRequestPackageInstalls()) {
                    provisionAndStartPendingEngine()
                } else {
                    finishPendingEngineFailure("Allow AppCompat to install its compatibility runtime, then try again.")
                }
            }
        }
    }

    private fun handleApkPickerResult(resultCode: Int, data: Intent?) {
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
            // ApkAnalyzer makes a private copy immediately, so a transient document
            // grant is sufficient for compatibility execution.
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

    private fun registerPendingGuestIfInstalled(launched: Boolean, installed: Boolean) {
        if (!launched && !installed) return
        EngineBroker.register(
            this,
            pendingEnginePackage,
            pendingEngineName.ifBlank { pendingEnginePackage },
            pendingEngineBits,
            pendingEngineApk?.absolutePath
        )
    }

    private fun handleEngineResult(resultCode: Int, data: Intent?) {
        val result = pendingEngineResult ?: return
        val operation = pendingEngineOperation
        val extras = linkedMapOf<String, Any?>()
        data?.extras?.keySet()?.forEach { key ->
            @Suppress("DEPRECATION")
            extras[key] = data.extras?.get(key)
        }

        val launched = extras["launched"] == true
        val installed = extras["installed"] == true
        val removed = extras["removed"] == true
        val message = extras["message"]?.toString()

        when (operation) {
            EngineOperation.RUN -> {
                // Installation is durable independently of first-screen success.
                registerPendingGuestIfInstalled(launched, installed)
                if (!extras.containsKey("launched")) extras["launched"] = resultCode == Activity.RESULT_OK
                extras["installed"] = installed
                extras["packageName"] = pendingEnginePackage
                extras["engineBits"] = pendingEngineBits
                if (!launched && installed) {
                    extras["message"] = message
                        ?: "The app was added to your Compatibility library, but its first-run screen did not complete. You can retry it from the library without importing the APK again."
                } else if (message == null && resultCode != Activity.RESULT_OK) {
                    extras["message"] = "The compatibility runtime returned without a successful launch."
                }
                result.success(extras)
            }
            EngineOperation.LIBRARY_RUN -> {
                registerPendingGuestIfInstalled(launched, installed)
                result.success(launched || resultCode == Activity.RESULT_OK)
            }
            EngineOperation.LAUNCH -> result.success(launched || resultCode == Activity.RESULT_OK)
            EngineOperation.REMOVE -> {
                if (removed || resultCode == Activity.RESULT_OK) {
                    EngineBroker.unregister(this, pendingEnginePackage)
                    result.success(null)
                } else {
                    result.error("remove_failed", message ?: "The compatibility app could not be removed.", extras)
                }
            }
            null -> result.error("engine_state_lost", "Compatibility operation state was lost.", null)
        }
        clearPendingEngine()
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
        if (resumeCount > 1 && !waitingForUnknownSources) maybeOpenNewlyInstalledApp()
    }

    private fun maybeOpenNewlyInstalledApp() {
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        val packageName = prefs.getString(PENDING_NATIVE_PACKAGE, null) ?: return
        val startedAt = prefs.getLong(PENDING_NATIVE_STARTED_AT, 0L)

        val installed = try {
            if (Build.VERSION.SDK_INT >= 33) {
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
            if (System.currentTimeMillis() - startedAt > 700L) {
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
        val matches = if (Build.VERSION.SDK_INT >= 33) {
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

    override fun onDestroy() {
        pendingPickerResult = null
        if (isFinishing) {
            pendingEngineResult = null
        }
        worker.shutdownNow()
        super.onDestroy()
    }
}
