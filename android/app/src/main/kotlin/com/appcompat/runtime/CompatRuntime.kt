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

                // Explicitly disable the upstream optional remote log sender.
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

            if (!core.isInstalled(expectedPackage, USER_ID)) {
                val install = core.installPackageAsUser(path, USER_ID)
                if (!install.success) {
                    return mapOf(
                        "launched" to false,
                        "message" to (install.msg ?: "Virtual installation failed."),
                        "packageName" to expectedPackage
                    )
                }
                if (!install.packageName.isNullOrBlank()) packageName = install.packageName
            }

            val launched = core.launchApk(packageName, USER_ID)
            mapOf(
                "launched" to launched,
                "packageName" to packageName,
                "message" to if (launched) "Launched" else "The virtual package installed but Android could not start its launch activity."
            )
        } catch (t: Throwable) {
            Log.e(TAG, "Virtual install/launch failed", t)
            mapOf("launched" to false, "message" to (t.message ?: t.javaClass.simpleName))
        }
    }

    fun launch(packageName: String): Boolean {
        if (!ready) return false
        return try {
            BlackBoxCore.get().launchApk(packageName, USER_ID)
        } catch (t: Throwable) {
            Log.e(TAG, "Launch failed for $packageName", t)
            false
        }
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
