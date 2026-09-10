package com.appcompat.runtime

import android.content.Context
import android.util.Log
import top.niunaijun.blackbox.BlackBoxCore
import top.niunaijun.blackbox.app.configuration.ClientConfiguration
import java.io.File

object CompatRuntime {
    private const val TAG = "AppCompatRuntime"
    private const val USER_ID = 0

    @Volatile
    var ready: Boolean = false
        private set

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

    fun create() {
        try {
            BlackBoxCore.get().doCreate()
            ready = true
        } catch (t: Throwable) {
            ready = false
            Log.e(TAG, "Compatibility engine failed to initialize", t)
        }
    }

    fun installAndLaunch(path: String, expectedPackage: String): Map<String, Any?> {
        if (!ready) {
            return mapOf("launched" to false, "message" to "Compatibility engine is not ready.")
        }
        return try {
            val core = BlackBoxCore.get()
            var packageName = expectedPackage
            val apkFile = File(path)
            if (!apkFile.isFile || !apkFile.canRead()) {
                return mapOf("launched" to false, "message" to "The imported APK file is no longer readable.")
            }

            if (!core.isInstalled(expectedPackage, USER_ID)) {
                val install = core.installPackageAsUser(apkFile, USER_ID)
                if (!install.success) {
                    return mapOf(
                        "launched" to false,
                        "message" to (install.msg ?: "Virtual installation failed."),
                        "packageName" to expectedPackage
                    )
                }
                if (!install.packageName.isNullOrBlank()) packageName = install.packageName
            }

            val launched = launchWithRetry(core, packageName)
            mapOf(
                "launched" to launched,
                "packageName" to packageName,
                "message" to if (launched) {
                    "Launched"
                } else {
                    "The virtual package installed, but its launch activity did not become ready after a retry. This app may depend on a system role or service that cannot be virtualized."
                }
            )
        } catch (t: Throwable) {
            Log.e(TAG, "Virtual install/launch failed", t)
            mapOf("launched" to false, "message" to (t.message ?: t.javaClass.simpleName))
        }
    }

    fun launch(packageName: String): Boolean {
        if (!ready) return false
        return try {
            launchWithRetry(BlackBoxCore.get(), packageName)
        } catch (t: Throwable) {
            Log.e(TAG, "Launch failed for $packageName", t)
            false
        }
    }

    private fun launchWithRetry(core: BlackBoxCore, packageName: String): Boolean {
        if (core.launchApk(packageName, USER_ID)) return true
        // Some very old apps register or restore virtual components immediately after
        // installation. Give the engine one short settling window instead of leaving
        // the user on a spinner or requiring another tap.
        try {
            Thread.sleep(450)
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
            return false
        }
        return core.launchApk(packageName, USER_ID)
    }

    fun remove(packageName: String) {
        if (!ready) return
        try {
            BlackBoxCore.get().uninstallPackageAsUser(packageName, USER_ID)
        } catch (t: Throwable) {
            Log.e(TAG, "Remove failed for $packageName", t)
        }
    }

    fun listApps(): List<Map<String, String>> {
        if (!ready) return emptyList()
        return try {
            val core = BlackBoxCore.get()
            val packageManager = BlackBoxCore.getPackageManager()
            core.getInstalledApplications(0, USER_ID)
                .filter { it.packageName != BlackBoxCore.getHostPkg() }
                .map { info ->
                    val label = try {
                        packageManager.getApplicationLabel(info).toString()
                    } catch (_: Throwable) {
                        info.packageName
                    }
                    mapOf("name" to label, "packageName" to info.packageName)
                }
                .sortedBy { it["name"]?.lowercase() }
        } catch (t: Throwable) {
            Log.e(TAG, "Could not enumerate virtual apps", t)
            emptyList()
        }
    }
}
