package com.appcompat.runtime

import android.app.Activity
import android.app.PendingIntent
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import androidx.core.content.FileProvider
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Routes a guest APK into an ABI-matched compatibility process.
 *
 * Android chooses one primary ABI for an installed package, so a 64-bit host
 * process cannot simply dlopen a 32-bit guest library (or vice versa). AppCompat
 * keeps the visible product as one APK, then provisions a hidden, signature-
 * protected runtime package for the required bitness on first use. This is the
 * same architectural boundary Android's dual zygotes impose at process level.
 */
object EngineBroker {
    const val ENGINE_PERMISSION = "com.appcompat.runtime.permission.ENGINE"
    const val ACTION_RUN = "com.appcompat.runtime.engine.RUN"
    const val ACTION_LAUNCH = "com.appcompat.runtime.engine.LAUNCH"
    const val ACTION_REMOVE = "com.appcompat.runtime.engine.REMOVE"
    const val ACTION_LAST_CRASH = "com.appcompat.runtime.engine.LAST_CRASH"
    const val BRIDGE_CLASS = "com.appcompat.engine.EngineBridgeActivity"

    // Keep helper package IDs versioned. A new namespace forces Android to install
    // the runtime carrying the matching framework-translation code even when an
    // older helper was signed by a different ephemeral CI certificate.
    private const val ENGINE32_PACKAGE = "com.appcompat.runtime.engine32.v7"
    private const val ENGINE64_PACKAGE = "com.appcompat.runtime.engine64.v7"
    private const val REGISTRY_PREFS = "appcompat_virtual_registry"
    private const val REGISTRY_JSON = "apps"

    // The embedded helper APKs intentionally use the same versionCode as the host.
    // Derive the required version from BuildConfig instead of maintaining a second
    // hard-coded integer so runtime fixes can never silently reuse a stale helper.
    private val requiredEngineVersion: Long
        get() = BuildConfig.VERSION_CODE.toLong()

    fun packageFor(bits: Int): String = if (bits == 32) ENGINE32_PACKAGE else ENGINE64_PACKAGE

    fun engineSupported(bits: Int): Boolean = when (bits) {
        32 -> Build.SUPPORTED_32_BIT_ABIS.isNotEmpty()
        64 -> Build.SUPPORTED_64_BIT_ABIS.isNotEmpty()
        else -> false
    }

    fun chooseBits(report: Map<String, Any?>): Int? {
        val abis = (report["abis"] as? List<*>)?.map { it.toString() }.orEmpty()
        val has32 = abis.any { it == "armeabi" || it == "armeabi-v7a" || it == "x86" }
        val has64 = abis.any { it == "arm64-v8a" || it == "x86_64" }

        // Pure-Dex APKs can run in either runtime. Prefer the device's primary
        // bitness so ART/framework behavior is closest to a normal installation.
        if (abis.isEmpty()) {
            return when {
                Build.SUPPORTED_64_BIT_ABIS.isNotEmpty() -> 64
                Build.SUPPORTED_32_BIT_ABIS.isNotEmpty() -> 32
                else -> null
            }
        }

        // For a fat APK prefer 64-bit, matching modern Android package selection.
        if (has64 && Build.SUPPORTED_64_BIT_ABIS.isNotEmpty()) return 64
        if (has32 && Build.SUPPORTED_32_BIT_ABIS.isNotEmpty()) return 32
        return null
    }

    fun status(context: Context): Map<String, Any?> = linkedMapOf(
        "engine32Supported" to engineSupported(32),
        "engine64Supported" to engineSupported(64),
        "engine32Installed" to isInstalled(context, 32),
        "engine64Installed" to isInstalled(context, 64),
        "engine32Version" to installedVersion(context, 32),
        "engine64Version" to installedVersion(context, 64),
        "requiredEngineVersion" to requiredEngineVersion,
        "singleApkRouting" to true
    )

    fun ensureInstalled(context: Context, bits: Int, callback: (Boolean, String?) -> Unit) {
        if (!engineSupported(bits)) {
            callback(false, "This device does not expose a ${bits}-bit Android application runtime.")
            return
        }
        if (isInstalled(context, bits)) {
            callback(true, null)
            return
        }

        val assetPath = if (bits == 32) "engines/appcompat-engine32.apk" else "engines/appcompat-engine64.apk"
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(packageFor(bits))
            if (Build.VERSION.SDK_INT >= 31) {
                setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_REQUIRED)
            }
        }

        var sessionId = -1
        try {
            sessionId = installer.createSession(params)
            installer.openSession(sessionId).use { session ->
                context.assets.open(assetPath).use { input ->
                    session.openWrite("base.apk", 0, -1).use { output ->
                        input.copyTo(output, 1024 * 1024)
                        session.fsync(output)
                    }
                }

                EngineInstallCoordinator.callback = { callbackBits, success, message ->
                    if (callbackBits == bits) callback(success, message)
                }
                val statusIntent = Intent(context, EngineInstallReceiver::class.java)
                    .putExtra("engineBits", bits)
                val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                    if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0
                val pending = PendingIntent.getBroadcast(context, bits, statusIntent, flags)
                session.commit(pending.intentSender)
            }
        } catch (t: Throwable) {
            EngineInstallCoordinator.callback = null
            if (sessionId >= 0) runCatching { installer.abandonSession(sessionId) }
            callback(false, t.message ?: "Could not provision the compatibility runtime.")
        }
    }

    fun start(
        activity: Activity,
        bits: Int,
        action: String,
        packageName: String,
        apkFile: File?,
        requestCode: Int
    ) {
        val enginePackage = packageFor(bits)
        val intent = Intent(action).apply {
            component = ComponentName(enginePackage, BRIDGE_CLASS)
            putExtra("packageName", packageName)
            putExtra("engineBits", bits)
            addFlags(Intent.FLAG_ACTIVITY_NO_ANIMATION)
        }

        if (apkFile != null) {
            val uri: Uri = FileProvider.getUriForFile(
                activity,
                "${activity.packageName}.files",
                apkFile
            )
            intent.data = uri
            intent.clipData = android.content.ClipData.newRawUri("legacy-apk", uri)
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            activity.grantUriPermission(enginePackage, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }

        activity.startActivityForResult(intent, requestCode)
    }

    @Suppress("DEPRECATION")
    fun isInstalled(context: Context, bits: Int): Boolean =
        installedVersion(context, bits) == requiredEngineVersion

    @Suppress("DEPRECATION")
    private fun installedVersion(context: Context, bits: Int): Long? {
        return try {
            val info = if (Build.VERSION.SDK_INT >= 33) {
                context.packageManager.getPackageInfo(
                    packageFor(bits),
                    PackageManager.PackageInfoFlags.of(0)
                )
            } else {
                context.packageManager.getPackageInfo(packageFor(bits), 0)
            }
            if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong()
        } catch (_: Throwable) {
            null
        }
    }

    fun register(context: Context, packageName: String, name: String, bits: Int) {
        val apps = readRegistry(context)
        val filtered = JSONArray()
        for (i in 0 until apps.length()) {
            val item = apps.optJSONObject(i) ?: continue
            if (item.optString("packageName") != packageName) filtered.put(item)
        }
        filtered.put(
            JSONObject()
                .put("packageName", packageName)
                .put("name", name.ifBlank { packageName })
                .put("engineBits", bits)
        )
        writeRegistry(context, filtered)
    }

    fun unregister(context: Context, packageName: String) {
        val apps = readRegistry(context)
        val filtered = JSONArray()
        for (i in 0 until apps.length()) {
            val item = apps.optJSONObject(i) ?: continue
            if (item.optString("packageName") != packageName) filtered.put(item)
        }
        writeRegistry(context, filtered)
    }

    fun registeredBits(context: Context, packageName: String): Int? {
        val apps = readRegistry(context)
        for (i in 0 until apps.length()) {
            val item = apps.optJSONObject(i) ?: continue
            if (item.optString("packageName") == packageName) {
                return item.optInt("engineBits").takeIf { it == 32 || it == 64 }
            }
        }
        return null
    }

    fun listRegistered(context: Context): List<Map<String, Any?>> {
        val apps = readRegistry(context)
        val out = mutableListOf<Map<String, Any?>>()
        for (i in 0 until apps.length()) {
            val item = apps.optJSONObject(i) ?: continue
            val bits = item.optInt("engineBits")
            if (!isInstalled(context, bits)) continue
            out += linkedMapOf(
                "packageName" to item.optString("packageName"),
                "name" to item.optString("name"),
                "engineBits" to bits
            )
        }
        return out
    }

    private fun readRegistry(context: Context): JSONArray {
        val raw = context.getSharedPreferences(REGISTRY_PREFS, Context.MODE_PRIVATE)
            .getString(REGISTRY_JSON, "[]") ?: "[]"
        return runCatching { JSONArray(raw) }.getOrElse { JSONArray() }
    }

    private fun writeRegistry(context: Context, array: JSONArray) {
        context.getSharedPreferences(REGISTRY_PREFS, Context.MODE_PRIVATE)
            .edit().putString(REGISTRY_JSON, array.toString()).apply()
    }
}
