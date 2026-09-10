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

            val launched = core.launchApk(packageName, USER_ID)
            linkedMapOf(
                "launched" to launched,
                "packageName" to packageName,
                "engineBits" to BuildConfig.ENGINE_BITS,
                "message" to if (launched) {
                    "Launched with the ${BuildConfig.ENGINE_BITS}-bit compatibility engine."
                } else {
                    "The app was imported but Android could not start its launch activity."
                }
            )
        } catch (t: Throwable) {
            Log.e(TAG, "Virtual install/launch failed", t)
            failure(t.message ?: t.javaClass.simpleName, expectedPackage)
        }
    }

    fun launch(packageName: String): Map<String, Any?> {
        if (!ready) return failure("Compatibility engine is not ready.", packageName)
        return try {
            val launched = BlackBoxCore.get().launchApk(packageName, USER_ID)
            linkedMapOf(
                "launched" to launched,
                "packageName" to packageName,
                "engineBits" to BuildConfig.ENGINE_BITS,
                "message" to if (launched) "Launched" else "The compatibility engine could not start this app."
            )
        } catch (t: Throwable) {
            Log.e(TAG, "Launch failed for $packageName", t)
            failure(t.message ?: t.javaClass.simpleName, packageName)
        }
    }

    fun remove(packageName: String): Map<String, Any?> {
        if (!ready) return failure("Compatibility engine is not ready.", packageName)
        return try {
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
        val file = File(context.filesDir, "last-guest-crash.json")
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
            File(context.filesDir, "last-guest-crash.json").writeText(json.toString())
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
