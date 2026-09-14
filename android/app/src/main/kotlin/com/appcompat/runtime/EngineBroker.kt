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

/** Routes a guest APK into an ABI-matched compatibility process. */
object EngineBroker {
    const val ENGINE_PERMISSION = "com.appcompat.runtime.permission.ENGINE"
    const val ACTION_RUN = "com.appcompat.runtime.engine.RUN"
    const val ACTION_LAUNCH = "com.appcompat.runtime.engine.LAUNCH"
    const val ACTION_REMOVE = "com.appcompat.runtime.engine.REMOVE"
    const val ACTION_LAST_CRASH = "com.appcompat.runtime.engine.LAST_CRASH"
    const val BRIDGE_CLASS = "com.appcompat.engine.EngineBridgeActivity"

    private const val ENGINE32_PACKAGE = "com.appcompat.runtime.engine32.v13"
    private const val ENGINE64_PACKAGE = "com.appcompat.runtime.engine64.v13"
    // Keep the r4 package identity so already-granted Android special access remains
    // attached to the same real UID/package. The payload versionName below forces an
    // in-place helper APK refresh when universal compatibility code changes.
    private const val ENGINE_PAYLOAD_SUFFIX = ".r4"
    private const val REQUIRED_ENGINE_VERSION_NAME = "0.13.1-universal"
    private const val FLOATIFY_GUEST_PACKAGE = "com.jamworks.floatify"
    private const val FLOATIFY_HELPER_SUFFIX = ".floatify"
    private const val COMPAT_PROFILE_TARGET_SDK = 25
    private const val REGISTRY_PREFS = "appcompat_virtual_registry"
    private const val REGISTRY_JSON = "apps"

    private val requiredEngineVersion: Long
        get() = BuildConfig.VERSION_CODE.toLong()

    /**
     * Android 11+ no longer allows ACTION_MANAGE_OVERLAY_PERMISSION to deep-link to
     * a particular package; it always shows the top-level app list. Floatify gets a
     * dedicated real helper package whose application label is "Floatify", so the
     * user can identify the correct permission principal without guessing which
     * generic AppCompat runtime belongs to the virtual app.
     */
    fun packageFor(bits: Int, guestPackage: String? = null): String {
        val base = if (bits == 32) ENGINE32_PACKAGE else ENGINE64_PACKAGE
        val guestSuffix = if (guestPackage == FLOATIFY_GUEST_PACKAGE) {
            FLOATIFY_HELPER_SUFFIX
        } else {
            ""
        }
        return base + ENGINE_PAYLOAD_SUFFIX + guestSuffix
    }

    private fun assetFor(bits: Int, guestPackage: String?): String {
        val prefix = if (guestPackage == FLOATIFY_GUEST_PACKAGE) "floatify" else "appcompat"
        return if (bits == 32) {
            "engines/${prefix}-engine32.apk"
        } else {
            "engines/${prefix}-engine64.apk"
        }
    }

    fun engineSupported(bits: Int): Boolean = when (bits) {
        32 -> Build.SUPPORTED_32_BIT_ABIS.isNotEmpty()
        64 -> Build.SUPPORTED_64_BIT_ABIS.isNotEmpty()
        else -> false
    }

    fun chooseBits(report: Map<String, Any?>): Int? {
        val abis = (report["abis"] as? List<*>)?.map { it.toString() }.orEmpty()
        val has32 = abis.any { it == "armeabi" || it == "armeabi-v7a" || it == "x86" }
        val has64 = abis.any { it == "arm64-v8a" || it == "x86_64" }

        if (abis.isEmpty()) {
            return when {
                Build.SUPPORTED_64_BIT_ABIS.isNotEmpty() -> 64
                Build.SUPPORTED_32_BIT_ABIS.isNotEmpty() -> 32
                else -> null
            }
        }
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
        "engine32VersionName" to installedVersionName(context, 32),
        "engine64VersionName" to installedVersionName(context, 64),
        "floatify32Installed" to isInstalled(context, 32, FLOATIFY_GUEST_PACKAGE),
        "floatify64Installed" to isInstalled(context, 64, FLOATIFY_GUEST_PACKAGE),
        "requiredEngineVersion" to requiredEngineVersion,
        "requiredEngineVersionName" to REQUIRED_ENGINE_VERSION_NAME,
        "compatProfileTargetSdk" to COMPAT_PROFILE_TARGET_SDK,
        "singleApkRouting" to true,
        "guestNamedPermissionHelpers" to true,
    )

    // Retain the original generic API for host status/bootstrap callers.
    fun ensureInstalled(context: Context, bits: Int, callback: (Boolean, String?) -> Unit) =
        ensureInstalled(context, bits, null, callback)

    fun ensureInstalled(
        context: Context,
        bits: Int,
        guestPackage: String?,
        callback: (Boolean, String?) -> Unit,
    ) {
        if (!engineSupported(bits)) {
            callback(false, "This device does not expose a ${bits}-bit Android application runtime.")
            return
        }
        if (isInstalled(context, bits, guestPackage)) {
            callback(true, null)
            return
        }

        val enginePackage = packageFor(bits, guestPackage)
        val assetPath = assetFor(bits, guestPackage)
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(enginePackage)
            if (guestPackage == FLOATIFY_GUEST_PACKAGE) {
                // This also makes the PackageInstaller confirmation UI identify the
                // helper by the virtual app name. The installed manifest carries the
                // same label for Special app access / notification access Settings.
                setAppLabel("Floatify")
            }
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
        requestCode: Int,
    ) {
        val enginePackage = packageFor(bits, packageName)
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
                apkFile,
            )
            intent.data = uri
            intent.clipData = android.content.ClipData.newRawUri("legacy-apk", uri)
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            activity.grantUriPermission(enginePackage, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }

        activity.startActivityForResult(intent, requestCode)
    }

    @Suppress("DEPRECATION")
    fun isInstalled(context: Context, bits: Int, guestPackage: String? = null): Boolean =
        installedVersion(context, bits, guestPackage) == requiredEngineVersion &&
            installedVersionName(context, bits, guestPackage) == REQUIRED_ENGINE_VERSION_NAME

    @Suppress("DEPRECATION")
    private fun installedVersion(
        context: Context,
        bits: Int,
        guestPackage: String? = null,
    ): Long? {
        return try {
            val helperPackage = packageFor(bits, guestPackage)
            val info = if (Build.VERSION.SDK_INT >= 33) {
                context.packageManager.getPackageInfo(
                    helperPackage,
                    PackageManager.PackageInfoFlags.of(0),
                )
            } else {
                context.packageManager.getPackageInfo(helperPackage, 0)
            }
            if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong()
        } catch (_: Throwable) {
            null
        }
    }

    @Suppress("DEPRECATION")
    private fun installedVersionName(
        context: Context,
        bits: Int,
        guestPackage: String? = null,
    ): String? {
        return try {
            val helperPackage = packageFor(bits, guestPackage)
            val info = if (Build.VERSION.SDK_INT >= 33) {
                context.packageManager.getPackageInfo(
                    helperPackage,
                    PackageManager.PackageInfoFlags.of(0),
                )
            } else {
                context.packageManager.getPackageInfo(helperPackage, 0)
            }
            info.versionName
        } catch (_: Throwable) {
            null
        }
    }

    /**
     * The library belongs to the visible AppCompat host, not to a particular helper
     * version. Keep the source APK path so a newer helper can transparently rebuild
     * its virtual install instead of making the user's library disappear.
     */
    fun register(
        context: Context,
        packageName: String,
        name: String,
        bits: Int,
        apkPath: String? = null,
    ) {
        val apps = readRegistry(context)
        var oldPath: String? = null
        val filtered = JSONArray()
        for (i in 0 until apps.length()) {
            val item = apps.optJSONObject(i) ?: continue
            if (item.optString("packageName") == packageName) {
                oldPath = item.optString("apkPath").takeIf { it.isNotBlank() }
            } else {
                filtered.put(item)
            }
        }
        filtered.put(
            JSONObject()
                .put("packageName", packageName)
                .put("name", name.ifBlank { packageName })
                .put("engineBits", bits)
                .put("apkPath", apkPath?.takeIf { it.isNotBlank() } ?: oldPath ?: ""),
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

    private fun registeredObject(context: Context, packageName: String): JSONObject? {
        val apps = readRegistry(context)
        for (i in 0 until apps.length()) {
            val item = apps.optJSONObject(i) ?: continue
            if (item.optString("packageName") == packageName) return item
        }
        return null
    }

    fun registeredBits(context: Context, packageName: String): Int? =
        registeredObject(context, packageName)
            ?.optInt("engineBits")
            ?.takeIf { it == 32 || it == 64 }

    /**
     * Older AppCompat builds did not store apkPath in the registry. Recover those
     * entries by matching the package name against the private imported APK cache.
     */
    fun registeredApkPath(context: Context, packageName: String): String? {
        val direct = registeredObject(context, packageName)
            ?.optString("apkPath")
            ?.takeIf { it.isNotBlank() && File(it).isFile }
        if (direct != null) return direct
        return findCachedSourceApk(context, packageName)?.absolutePath
    }

    fun registeredName(context: Context, packageName: String): String =
        registeredObject(context, packageName)
            ?.optString("name")
            ?.takeIf { it.isNotBlank() }
            ?: packageName

    @Suppress("DEPRECATION")
    private fun findCachedSourceApk(context: Context, packageName: String): File? {
        val directory = File(context.filesDir, "imported-apks")
        val candidates = directory.listFiles { file ->
            file.isFile && file.extension.equals("apk", ignoreCase = true)
        }?.sortedByDescending { it.lastModified() }.orEmpty()

        for (candidate in candidates) {
            val parsed = runCatching {
                context.packageManager.getPackageArchiveInfo(candidate.absolutePath, 0)
            }.getOrNull()
            if (parsed?.packageName == packageName) return candidate
        }
        return null
    }

    fun listRegistered(context: Context): List<Map<String, Any?>> {
        val apps = readRegistry(context)
        val out = mutableListOf<Map<String, Any?>>()
        for (i in 0 until apps.length()) {
            val item = apps.optJSONObject(i) ?: continue
            val bits = item.optInt("engineBits")
            if (bits != 32 && bits != 64) continue
            val packageName = item.optString("packageName")
            out += linkedMapOf(
                "packageName" to packageName,
                "name" to item.optString("name"),
                "engineBits" to bits,
                "runtimeInstalled" to isInstalled(context, bits, packageName),
                "hasSourceApk" to (registeredApkPath(context, packageName) != null),
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
