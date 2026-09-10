package com.appcompat.engine

import android.content.Context
import android.net.Uri
import android.os.Process
import android.util.Log
import org.json.JSONObject
import top.niunaijun.blackbox.BlackBoxCore
import top.niunaijun.blackbox.app.configuration.ClientConfiguration
import java.io.File
import java.io.FileOutputStream

object EngineRuntime {
    private const val TAG = "AppCompatEngine"
    private const val USER_ID = 0
    private const val CRASH_FILE = "last-guest-crash.json"

    @Volatile
    var ready: Boolean = false
        private set

    private var appContext: Context? = null

    fun attachBaseContext(context: Context) {
        BlackBoxCore.get().doAttachBaseContext(
            context,
            object : ClientConfiguration() {
                override fun getHostPackageName(): String = context.packageName
                override fun isHideRoot(): Boolean = false
                override fun isEnableDaemonService(): Boolean = false
                override fun isEnableLauncherActivity(): Boolean = false
                override fun isUseVpnNetwork(): Boolean = false
                override fun isDisableFlagSecure(): Boolean = false
                override fun requestInstallPackage(file: File?, userId: Int): Boolean = false
                override fun getLogSenderChatId(): String = ""
            }
        )
    }

    fun create(context: Context) {
        appContext = context.applicationContext
        try {
            BlackBoxCore.get().doCreate()
            val actualBits = if (Process.is64Bit()) 64 else 32
            if (actualBits != BuildConfig.ENGINE_BITS) {
                ready = false
                Log.e(TAG, "Engine ABI mismatch: expected ${BuildConfig.ENGINE_BITS}-bit but process is $actualBits-bit")
                return
            }

            // This handler is installed in every helper/guest process that loads the
            // host Application, so a crash can be reported back instead of leaving the
            // user with a frozen splash screen and no explanation.
            BlackBoxCore.get().setExceptionHandler { thread, throwable ->
                persistCrash(thread, throwable)
            }
            ready = true
            Log.i(TAG, "${BuildConfig.ENGINE_BITS}-bit compatibility engine ready")
        } catch (t: Throwable) {
            ready = false
            Log.e(TAG, "Compatibility engine failed to initialize", t)
        }
    }

    fun installAndLaunch(context: Context, uriString: String, expectedPackage: String): Map<String, Any?> {
        if (!ready) {
            return failure("The ${BuildConfig.ENGINE_BITS}-bit compatibility engine is not ready.", expectedPackage)
        }
        return try {
            val apkFile = importApk(context, uriString, expectedPackage)
            val core = BlackBoxCore.get()
            var packageName = expectedPackage

            if (!core.isInstalled(expectedPackage, USER_ID)) {
                val install = core.installPackageAsUser(apkFile, USER_ID)
                if (!install.success) {
                    return failure(install.msg ?: "Virtual installation failed.", expectedPackage)
                }
                if (!install.packageName.isNullOrBlank()) packageName = install.packageName
            }

            launchWithRecovery(packageName, freshImport = true)
        } catch (t: Throwable) {
            Log.e(TAG, "Virtual install/launch failed", t)
            failure(t.message ?: t.javaClass.simpleName, expectedPackage)
        }
    }

    fun launch(packageName: String): Map<String, Any?> {
        if (!ready) return failure("Compatibility engine is not ready.", packageName)
        return try {
            launchWithRecovery(packageName, freshImport = false)
        } catch (t: Throwable) {
            Log.e(TAG, "Launch failed for $packageName", t)
            failure(t.message ?: t.javaClass.simpleName, packageName)
        }
    }

    /**
     * BlackBox launch is asynchronous: returning true only means the proxy activity
     * was scheduled. A number of old apps crash immediately after their first frame.
     * Detect that short crash window, stop the stale guest process and retry once.
     * We never wipe user data automatically.
     */
    private fun launchWithRecovery(packageName: String, freshImport: Boolean): Map<String, Any?> {
        val context = appContext ?: return failure("Compatibility engine context is unavailable.", packageName)
        val core = BlackBoxCore.get()
        clearCrash(context)

        var retryCount = 0
        var launched = core.launchApk(packageName, USER_ID)
        if (!launched) {
            retryCount++
            runCatching { core.stopPackage(packageName, USER_ID) }
            Thread.sleep(300)
            launched = core.launchApk(packageName, USER_ID)
        }

        if (!launched) {
            return linkedMapOf(
                "launched" to false,
                "packageName" to packageName,
                "engineBits" to BuildConfig.ENGINE_BITS,
                "retryCount" to retryCount,
                "message" to "The compatibility engine could not resolve or start the app's launch activity."
            )
        }

        // Give Application.onCreate()/first Activity enough time to expose an
        // immediate framework/JNI crash. This is bounded so launching stays snappy.
        val firstStartedAt = System.currentTimeMillis()
        Thread.sleep(if (freshImport) 1400 else 1000)
        var crash = recentCrash(context, packageName, firstStartedAt)

        if (crash != null) {
            retryCount++
            Log.w(TAG, "Immediate guest crash detected for $packageName; retrying once")
            runCatching { core.stopPackage(packageName, USER_ID) }
            clearCrash(context)
            Thread.sleep(350)
            val retryStartedAt = System.currentTimeMillis()
            launched = core.launchApk(packageName, USER_ID)
            if (launched) {
                Thread.sleep(1100)
                crash = recentCrash(context, packageName, retryStartedAt)
            }
        }

        if (crash != null) {
            return linkedMapOf(
                "launched" to false,
                "crashDetected" to true,
                "packageName" to packageName,
                "engineBits" to BuildConfig.ENGINE_BITS,
                "retryCount" to retryCount,
                "exception" to crash.optString("exception"),
                "crashMessage" to crash.optString("message"),
                "message" to buildString {
                    append("The legacy app starts but crashes during initialization")
                    val exception = crash.optString("exception")
                    val detail = crash.optString("message")
                    if (exception.isNotBlank()) append(" ($exception)")
                    if (detail.isNotBlank()) append(": $detail")
                }
            )
        }

        return linkedMapOf(
            "launched" to true,
            "packageName" to packageName,
            "engineBits" to BuildConfig.ENGINE_BITS,
            "retryCount" to retryCount,
            "message" to if (retryCount > 0) {
                "Launched after compatibility runtime recovery."
            } else {
                "Launched with the ${BuildConfig.ENGINE_BITS}-bit compatibility runtime."
            }
        )
    }

    fun remove(packageName: String): Map<String, Any?> {
        if (!ready) return failure("Compatibility engine is not ready.", packageName)
        return try {
            runCatching { BlackBoxCore.get().stopPackage(packageName, USER_ID) }
            BlackBoxCore.get().uninstallPackageAsUser(packageName, USER_ID)
            linkedMapOf(
                "removed" to true,
                "packageName" to packageName,
                "engineBits" to BuildConfig.ENGINE_BITS,
                "message" to "Removed"
            )
        } catch (t: Throwable) {
            Log.e(TAG, "Remove failed for $packageName", t)
            linkedMapOf(
                "removed" to false,
                "packageName" to packageName,
                "engineBits" to BuildConfig.ENGINE_BITS,
                "message" to (t.message ?: t.javaClass.simpleName)
            )
        }
    }

    fun lastCrash(): Map<String, Any?> {
        val context = appContext ?: return emptyMap()
        val file = File(context.filesDir, CRASH_FILE)
        if (!file.isFile) return emptyMap()
        return try {
            val json = JSONObject(file.readText())
            linkedMapOf(
                "packageName" to json.optString("packageName"),
                "thread" to json.optString("thread"),
                "exception" to json.optString("exception"),
                "message" to json.optString("message"),
                "stack" to json.optString("stack"),
                "timestamp" to json.optLong("timestamp"),
                "engineBits" to BuildConfig.ENGINE_BITS
            )
        } catch (_: Throwable) {
            emptyMap()
        }
    }

    private fun importApk(context: Context, uriString: String, expectedPackage: String): File {
        val uri = Uri.parse(uriString)
        val importDir = File(context.filesDir, "imported-apks").apply { mkdirs() }
        val safeName = expectedPackage.replace(Regex("[^A-Za-z0-9._-]"), "_")
        val destination = File(importDir, "$safeName.apk")
        context.contentResolver.openInputStream(uri)?.use { input ->
            FileOutputStream(destination, false).buffered(1024 * 1024).use { output ->
                input.copyTo(output, 1024 * 1024)
            }
        } ?: error("Android could not read the selected APK in the compatibility engine.")
        if (!destination.isFile || destination.length() == 0L) error("The imported APK copy is empty.")
        return destination
    }

    private fun clearCrash(context: Context) {
        runCatching { File(context.filesDir, CRASH_FILE).delete() }
    }

    private fun recentCrash(context: Context, packageName: String, startedAt: Long): JSONObject? {
        val file = File(context.filesDir, CRASH_FILE)
        if (!file.isFile) return null
        return try {
            val json = JSONObject(file.readText())
            val timestamp = json.optLong("timestamp", 0L)
            val recordedPackage = json.optString("packageName")
            if (timestamp >= startedAt - 100L &&
                (recordedPackage.isBlank() || recordedPackage == packageName)) json else null
        } catch (_: Throwable) {
            null
        }
    }

    private fun persistCrash(thread: Thread, throwable: Throwable) {
        val context = appContext ?: return
        try {
            val stack = throwable.stackTraceToString().take(24_000)
            val json = JSONObject()
                .put("packageName", try { BlackBoxCore.getAppPackageName() ?: "" } catch (_: Throwable) { "" })
                .put("thread", thread.name)
                .put("exception", throwable.javaClass.name)
                .put("message", throwable.message ?: "")
                .put("stack", stack)
                .put("timestamp", System.currentTimeMillis())
            File(context.filesDir, CRASH_FILE).writeText(json.toString())
        } catch (t: Throwable) {
            Log.w(TAG, "Could not persist guest crash", t)
        }
    }

    private fun failure(message: String, packageName: String): Map<String, Any?> = linkedMapOf(
        "launched" to false,
        "packageName" to packageName,
        "engineBits" to BuildConfig.ENGINE_BITS,
        "message" to message
    )
}
