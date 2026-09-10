package com.appcompat.runtime

import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import java.io.File
import java.io.FileOutputStream
import java.util.zip.ZipFile

object ApkAnalyzer {
    @Suppress("DEPRECATION")
    fun analyze(context: Context, uri: Uri): Map<String, Any?> {
        val importDir = File(context.filesDir, "imported-apks").apply { mkdirs() }
        val apk = File(importDir, "import-${System.currentTimeMillis()}.apk")

        context.contentResolver.openInputStream(uri)?.use { input ->
            FileOutputStream(apk).buffered(1024 * 1024).use { output ->
                input.copyTo(output, 1024 * 1024)
            }
        } ?: error("Android could not open the selected APK.")

        val pm = context.packageManager
        val packageInfo = pm.getPackageArchiveInfo(apk.absolutePath, PackageManager.GET_PERMISSIONS)
            ?: run {
                apk.delete()
                error("This file is not a readable Android APK.")
            }

        val appInfo = packageInfo.applicationInfo ?: run {
            apk.delete()
            error("The APK does not contain valid application metadata.")
        }
        appInfo.sourceDir = apk.absolutePath
        appInfo.publicSourceDir = apk.absolutePath

        val name = try {
            pm.getApplicationLabel(appInfo).toString()
        } catch (_: Throwable) {
            packageInfo.packageName
        }

        val targetSdk = appInfo.targetSdkVersion
        val minSdk = if (Build.VERSION.SDK_INT >= 24) appInfo.minSdkVersion else 0
        val hostSdk = Build.VERSION.SDK_INT
        val requestedPermissions = packageInfo.requestedPermissions?.toList().orEmpty()

        val abis = linkedSetOf<String>()
        var dexCount = 0
        ZipFile(apk).use { zip ->
            val entries = zip.entries()
            while (entries.hasMoreElements()) {
                val entry = entries.nextElement()
                val nameInZip = entry.name
                if (nameInZip.matches(Regex("classes(\\d*)\\.dex"))) dexCount++
                val match = Regex("^lib/([^/]+)/.+\\.so$").find(nameInZip)
                if (match != null) abis += match.groupValues[1]
            }
        }

        val hostAbis = Build.SUPPORTED_ABIS.toSet()
        val nativeCompatible = abis.isEmpty() || abis.any { it in hostAbis }
        val has32 = abis.any { it == "armeabi" || it == "armeabi-v7a" || it == "x86" }
        val has64 = abis.any { it == "arm64-v8a" || it == "x86_64" }
        val strict64BitBlock = has32 && !has64 && Build.SUPPORTED_32_BIT_ABIS.isEmpty()

        // Android 14 blocks target < 23. Android 15+ raises that floor to target < 24.
        val lowTargetBlocked = when {
            hostSdk >= 35 -> targetSdk < 24
            hostSdk >= 34 -> targetSdk < 23
            else -> false
        }

        val minSdkBlock = minSdk > 0 && minSdk > hostSdk
        val legacyStorage = targetSdk <= 28 && requestedPermissions.any {
            it == "android.permission.READ_EXTERNAL_STORAGE" ||
                it == "android.permission.WRITE_EXTERNAL_STORAGE"
        }

        val issues = mutableListOf<String>()
        if (lowTargetBlocked) {
            issues += "Modern Android blocks normal installation at this target SDK; the compatibility runtime avoids the normal package-install path."
        }
        if (strict64BitBlock) {
            issues += "This APK contains only 32-bit native code, while this device exposes no 32-bit Android runtime. An ARM32 binary translator is still required."
        } else if (!nativeCompatible) {
            issues += "The APK native libraries do not match any CPU ABI exposed by this device."
        }
        if (legacyStorage) {
            issues += "The app uses legacy external-storage behavior. AppCompat will isolate its filesystem, but some hard-coded paths may still fail."
        }
        if (targetSdk in 1..22) {
            issues += "Very old framework behavior detected (target API $targetSdk); service and permission translation will be used."
        } else if (targetSdk in 23..28) {
            issues += "Legacy Android behavior detected (target API $targetSdk); virtual framework hooks are recommended."
        }
        if (minSdkBlock) {
            issues += "The APK declares a newer minimum Android API than this host device provides."
        }
        if (requestedPermissions.any { it == "android.permission.GET_ACCOUNTS" || it == "android.permission.USE_CREDENTIALS" }) {
            issues += "The app depends on legacy account APIs; account-backed sign-in may require services that no longer exist."
        }

        val route = when {
            minSdkBlock || strict64BitBlock || !nativeCompatible -> "limited"
            lowTargetBlocked || targetSdk <= 28 -> "virtual"
            else -> "native"
        }

        val confidence = when {
            route == "limited" -> "Limited — unresolved CPU or platform requirement"
            !abis.isEmpty() -> "Medium — native code can introduce device-specific failures"
            targetSdk <= 28 -> "High for self-contained Java/Kotlin apps"
            else -> "High — normal Android execution is also available"
        }

        return linkedMapOf(
            "name" to name,
            "packageName" to packageInfo.packageName,
            "versionName" to (packageInfo.versionName ?: "—"),
            "path" to apk.absolutePath,
            "uri" to uri.toString(),
            "targetSdk" to targetSdk,
            "minSdk" to minSdk,
            "hostSdk" to hostSdk,
            "sizeBytes" to apk.length(),
            "abis" to abis.toList(),
            "hostAbis" to Build.SUPPORTED_ABIS.toList(),
            "dexCount" to dexCount,
            "hasNativeCode" to abis.isNotEmpty(),
            "abiCompatible" to (nativeCompatible && !strict64BitBlock),
            "lowTargetBlocked" to lowTargetBlocked,
            "route" to route,
            "confidence" to confidence,
            "issues" to issues,
            "permissionCount" to requestedPermissions.size
        )
    }
}
