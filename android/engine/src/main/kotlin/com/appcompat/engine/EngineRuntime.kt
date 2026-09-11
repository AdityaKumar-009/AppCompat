package com.appcompat.engine

import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Process
import android.util.Log
import org.json.JSONObject
import top.niunaijun.blackbox.BlackBoxCore
import top.niunaijun.blackbox.app.configuration.ClientConfiguration
import top.niunaijun.blackbox.utils.compat.LegacyPermissionCompat
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

            // Installed in helper and guest processes. Persist the complete causal
            // chain so AppCompat can distinguish an engine bootstrap failure from a
            // crash thrown by the legacy app itself.
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

    /**
     * Performs only the virtual package installation. Permission prompts and guest
     * launch are deliberately separate because Android runtime-permission dialogs
     * must be driven by a real Activity on the helper package's UID.
     */
    fun install(context: Context, uriString: String, expectedPackage: String): Map<String, Any?> {
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

            linkedMapOf(
                "installed" to true,
                "packageName" to packageName,
                "engineBits" to BuildConfig.ENGINE_BITS,
                "compatibilityCode" to "INSTALLED",
                "message" to "Installed in the compatibility runtime."
            )
        } catch (t: Throwable) {
            Log.e(TAG, "Virtual install failed", t)
            failure(t.message ?: t.javaClass.simpleName, expectedPackage)
        }
    }

    /** Permissions Android must grant to the real helper UID for this virtual app. */
    fun requiredHostRuntimePermissions(packageName: String): List<String> {
        if (!ready || packageName.isBlank()) return emptyList()
        return try {
            LegacyPermissionCompat.requiredHostRuntimePermissions(packageName, USER_ID).toList()
        } catch (t: Throwable) {
            Log.w(TAG, "Could not calculate permission preflight for $packageName", t)
            emptyList()
        }
    }

    fun installAndLaunch(context: Context, uriString: String, expectedPackage: String): Map<String, Any?> {
        val installed = install(context, uriString, expectedPackage)
        if (installed["installed"] != true) return installed
        val packageName = installed["packageName"]?.toString().orEmpty().ifBlank { expectedPackage }
        return launchInstalled(packageName, freshImport = true)
    }

    fun launch(packageName: String): Map<String, Any?> {
        if (!ready) return failure("Compatibility engine is not ready.", packageName)
        return launchInstalled(packageName, freshImport = false)
    }

    fun launchInstalled(packageName: String, freshImport: Boolean): Map<String, Any?> {
        if (!ready) return failure("Compatibility engine is not ready.", packageName)
        return try {
            val result = launchWithRecovery(packageName, freshImport).toMutableMap()
            // Once a package has reached this path it exists in the virtual package
            // manager even if its welcome/onboarding Activity later crashes.
            result["installed"] = BlackBoxCore.get().isInstalled(packageName, USER_ID)
            result
        } catch (t: Throwable) {
            Log.e(TAG, "Launch failed for $packageName", t)
            failure(t.message ?: t.javaClass.simpleName, packageName).toMutableMap().apply {
                put("installed", runCatching { BlackBoxCore.get().isInstalled(packageName, USER_ID) }.getOrDefault(false))
            }
        }
    }

    /**
     * BlackBox launch is asynchronous: returning true only means the proxy activity
     * was scheduled. Detect the immediate Application/first-Activity crash window,
     * stop the stale process and retry once. Guest data is never wiped automatically.
     */
    private fun launchWithRecovery(packageName: String, freshImport: Boolean): Map<String, Any?> {
        val context = appContext ?: return failure("Compatibility engine context is unavailable.", packageName)
        val core = BlackBoxCore.get()
        clearCrash(context)

        // A newly selected APK may otherwise attach to a helper process left alive by
        // an earlier attempt/version. A process-only cold start is safe: virtual app
        // files, databases and preferences remain untouched.
        if (freshImport) {
            runCatching { core.stopPackage(packageName, USER_ID) }
            Thread.sleep(180)
        }

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
                "compatibilityCode" to "LAUNCH_ACTIVITY_UNRESOLVED",
                "message" to "The compatibility engine could not resolve or start the app's launch activity."
            )
        }

        val firstStartedAt = System.currentTimeMillis()
        // Permission and first-run SDK initializers often run shortly after the first
        // frame. Keep this window long enough to catch those deterministic crashes
        // without blocking normal interaction for an excessive period.
        Thread.sleep(if (freshImport) 2200 else 1500)
        var crash = recentCrash(context, packageName, firstStartedAt)

        if (crash != null) {
            retryCount++
            Log.w(TAG, "Immediate guest crash detected for $packageName; performing cold-process retry")
            runCatching { core.stopPackage(packageName, USER_ID) }
            clearCrash(context)
            Thread.sleep(400)
            val retryStartedAt = System.currentTimeMillis()
            launched = core.launchApk(packageName, USER_ID)
            if (launched) {
                Thread.sleep(1700)
                crash = recentCrash(context, packageName, retryStartedAt)
            }
        }

        if (crash != null) {
            val code = classifyCrash(crash)
            val rootException = crash.optString("rootException")
            val rootMessage = crash.optString("rootMessage")
            return linkedMapOf(
                "launched" to false,
                "crashDetected" to true,
                "packageName" to packageName,
                "engineBits" to BuildConfig.ENGINE_BITS,
                "retryCount" to retryCount,
                "compatibilityCode" to code,
                "exception" to crash.optString("exception"),
                "crashMessage" to crash.optString("message"),
                "rootException" to rootException,
                "rootMessage" to rootMessage,
                "diagnosticStack" to crash.optString("stack"),
                "message" to crashMessage(code, rootException, rootMessage)
            )
        }

        return linkedMapOf(
            "launched" to true,
            "packageName" to packageName,
            "engineBits" to BuildConfig.ENGINE_BITS,
            "retryCount" to retryCount,
            "compatibilityCode" to "OK",
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
                "rootException" to json.optString("rootException"),
                "rootMessage" to json.optString("rootMessage"),
                "compatibilityCode" to classifyCrash(json),
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
            val root = deepestCause(throwable)
            val stack = throwable.stackTraceToString().take(32_000)
            val json = JSONObject()
                .put("packageName", try { BlackBoxCore.getAppPackageName() ?: "" } catch (_: Throwable) { "" })
                .put("thread", thread.name)
                .put("exception", throwable.javaClass.name)
                .put("message", throwable.message ?: "")
                .put("rootException", root.javaClass.name)
                .put("rootMessage", root.message ?: "")
                .put("stack", stack)
                .put("timestamp", System.currentTimeMillis())
            File(context.filesDir, CRASH_FILE).writeText(json.toString())
        } catch (t: Throwable) {
            Log.w(TAG, "Could not persist guest crash", t)
        }
    }

    private fun deepestCause(throwable: Throwable): Throwable {
        var current = throwable
        val seen = HashSet<Throwable>()
        seen += current
        while (current.cause != null && current.cause !== current && seen.add(current.cause!!)) {
            current = current.cause!!
        }
        return current
    }

    private fun classifyCrash(crash: JSONObject): String {
        val haystack = buildString {
            append(crash.optString("exception")); append('\n')
            append(crash.optString("message")); append('\n')
            append(crash.optString("rootException")); append('\n')
            append(crash.optString("rootMessage")); append('\n')
            append(crash.optString("stack"))
        }
        return when {
            haystack.contains("Permission Denial", ignoreCase = true) ||
                haystack.contains("requires android.permission", ignoreCase = true) ||
                haystack.contains("not allowed to access", ignoreCase = true) -> "PERMISSION_TRANSLATION"
            haystack.contains("ClassCastException", ignoreCase = true) ||
                haystack.contains("BadParcelableException", ignoreCase = true) ||
                haystack.contains("NoSuchMethodError", ignoreCase = true) ||
                haystack.contains("AbstractMethodError", ignoreCase = true) ||
                haystack.contains("IncompatibleClassChangeError", ignoreCase = true) -> "FRAMEWORK_TRANSLATION"
            haystack.contains("Unable to makeApplication", ignoreCase = true) ||
                haystack.contains("makeApplication", ignoreCase = true) &&
                haystack.contains("ClassCastException", ignoreCase = true) -> "APPLICATION_BOOTSTRAP"
            haystack.contains("UnsatisfiedLinkError", ignoreCase = true) ||
                haystack.contains("dlopen failed", ignoreCase = true) -> "NATIVE_LIBRARY"
            haystack.contains("ClassNotFoundException", ignoreCase = true) ||
                haystack.contains("NoClassDefFoundError", ignoreCase = true) -> "MISSING_CLASS"
            haystack.contains("VerifyError", ignoreCase = true) -> "BYTECODE_VERIFY"
            haystack.contains("SecurityException", ignoreCase = true) -> "PLATFORM_SECURITY"
            else -> "GUEST_INITIALIZATION"
        }
    }

    private fun crashMessage(code: String, rootException: String, rootMessage: String): String {
        val detail = when {
            rootMessage.isNotBlank() -> rootMessage
            rootException.isNotBlank() -> rootException.substringAfterLast('.')
            else -> "No deeper cause was reported."
        }
        return when (code) {
            "PERMISSION_TRANSLATION" -> "The app reached a permission-gated Android API during onboarding. AppCompat translated the request, but Android still denied the required capability. Root cause: $detail"
            "FRAMEWORK_TRANSLATION" -> "The app hit a framework signature/type mismatch during onboarding. Root cause: $detail"
            "APPLICATION_BOOTSTRAP" -> "Application bootstrap still failed after AppCompat's recovery ladder. Root cause: $detail"
            "NATIVE_LIBRARY" -> "The app reached native-code loading but a required library could not be loaded. Root cause: $detail"
            "MISSING_CLASS" -> "The app expects a framework/library class that is unavailable in this runtime. Root cause: $detail"
            "BYTECODE_VERIFY" -> "Android rejected legacy bytecode during verification. Root cause: $detail"
            "PLATFORM_SECURITY" -> "A modern Android security boundary rejected an operation during app startup. Root cause: $detail"
            else -> "The legacy app itself still crashes during initialization after a cold-process retry. Root cause: $detail"
        }
    }

    private fun failure(message: String, packageName: String): Map<String, Any?> = linkedMapOf(
        "launched" to false,
        "installed" to false,
        "packageName" to packageName,
        "engineBits" to BuildConfig.ENGINE_BITS,
        "compatibilityCode" to "ENGINE_FAILURE",
        "message" to message
    )
}
