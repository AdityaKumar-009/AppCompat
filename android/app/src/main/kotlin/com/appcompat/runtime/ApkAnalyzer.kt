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
        val infoFlags = PackageManager.GET_PERMISSIONS or
            PackageManager.GET_ACTIVITIES or
            PackageManager.GET_SERVICES or
            PackageManager.GET_RECEIVERS or
            PackageManager.GET_PROVIDERS
        val packageInfo = pm.getPackageArchiveInfo(apk.absolutePath, infoFlags)
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
        val hasArm32 = abis.any { it == "armeabi" || it == "armeabi-v7a" }
        val hasArm64 = abis.contains("arm64-v8a")
        val hasX86 = abis.contains("x86")
        val hasX8664 = abis.contains("x86_64")
        val has32 = hasArm32 || hasX86
        val has64 = hasArm64 || hasX8664

        // Android 14 blocks target < 23. Android 15+ raises that floor to target < 24.
        val lowTargetBlocked = when {
            hostSdk >= 35 -> targetSdk < 24
            hostSdk >= 34 -> targetSdk < 23
            else -> false
        }
        val minSdkBlock = minSdk > 0 && minSdk > hostSdk
        val deviceHasNo32BitRuntime = has32 && !has64 && Build.SUPPORTED_32_BIT_ABIS.isEmpty()
        val deviceHasNo64BitRuntime = has64 && !has32 && Build.SUPPORTED_64_BIT_ABIS.isEmpty()

        // Helper runtimes currently support ARM Android guests. Pure Java/Kotlin APKs
        // are architecture neutral. x86-only native code on an ARM phone requires CPU
        // translation/full emulation and must not be presented as a normal virtual run.
        val helperNativeCompatible = when {
            abis.isEmpty() -> true
            hasArm64 && Build.SUPPORTED_64_BIT_ABIS.any { it == "arm64-v8a" } -> true
            hasArm32 && Build.SUPPORTED_32_BIT_ABIS.any { it == "armeabi-v7a" || it == "armeabi" } -> true
            else -> false
        }

        val packageLower = packageInfo.packageName.lowercase()
        val labelLower = name.lowercase()
        val integrationPermissionHints = setOf(
            "android.permission.SET_WALLPAPER",
            "android.permission.SET_WALLPAPER_HINTS",
            "android.permission.EXPAND_STATUS_BAR",
            "android.permission.REORDER_TASKS",
            "android.permission.GET_TASKS",
            "com.android.launcher.permission.INSTALL_SHORTCUT",
            "com.android.launcher.permission.UNINSTALL_SHORTCUT"
        )
        val boundServicePermissions = packageInfo.services.orEmpty().mapNotNull { it.permission }.toSet()
        val privilegedServiceHints = setOf(
            "android.permission.BIND_INPUT_METHOD",
            "android.permission.BIND_VPN_SERVICE",
            "android.permission.BIND_ACCESSIBILITY_SERVICE",
            "android.permission.BIND_WALLPAPER",
            "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE"
        )
        val systemIntegrated =
            packageLower.contains("launcher") || labelLower.contains("launcher") ||
            packageLower.contains("keyboard") || labelLower.contains("keyboard") ||
            requestedPermissions.any { it in integrationPermissionHints } ||
            boundServicePermissions.any { it in privilegedServiceHints }

        val nativeInstallable = !lowTargetBlocked && !minSdkBlock && nativeCompatible &&
            !deviceHasNo32BitRuntime && !deviceHasNo64BitRuntime
        val virtualCompatible = !minSdkBlock && helperNativeCompatible &&
            !deviceHasNo32BitRuntime && !deviceHasNo64BitRuntime

        val preferredEngineBits = when {
            abis.isEmpty() && Build.SUPPORTED_64_BIT_ABIS.isNotEmpty() -> 64
            abis.isEmpty() && Build.SUPPORTED_32_BIT_ABIS.isNotEmpty() -> 32
            hasArm64 && Build.SUPPORTED_64_BIT_ABIS.any { it == "arm64-v8a" } -> 64
            hasArm32 && Build.SUPPORTED_32_BIT_ABIS.any { it == "armeabi-v7a" || it == "armeabi" } -> 32
            else -> 0
        }

        val legacyStorage = targetSdk <= 28 && requestedPermissions.any {
            it == "android.permission.READ_EXTERNAL_STORAGE" ||
                it == "android.permission.WRITE_EXTERNAL_STORAGE"
        }

        val issues = mutableListOf<String>()
        if (nativeInstallable && targetSdk <= 28) {
            issues += "Android can still execute this APK directly. Native execution is preferred because it preserves real platform services, roles, widgets, launchers, accounts and OEM integrations."
        }
        if (systemIntegrated && nativeInstallable) {
            issues += "This app depends on system-level integration, so AppCompat will prefer Android's native install path instead of a sandboxed framework clone."
        }
        if (lowTargetBlocked) {
            issues += "Modern Android blocks normal installation at this target SDK. AppCompat will route it to an ABI-matched compatibility runtime when the device can host that CPU architecture."
        }
        if (deviceHasNo32BitRuntime) {
            issues += "This APK contains only 32-bit native code, but the device exposes no 32-bit Android application runtime. Full CPU translation or a legacy guest OS is required."
        } else if (deviceHasNo64BitRuntime) {
            issues += "This APK contains only 64-bit native code, but the device exposes no compatible 64-bit application runtime."
        } else if (!nativeCompatible) {
            issues += "The APK native libraries do not match an ABI Android exposes on this device."
        }
        if (!helperNativeCompatible && !nativeInstallable && !abis.isEmpty()) {
            issues += "This APK needs instruction-set translation that the lightweight compatibility engine does not provide."
        }
        if (systemIntegrated && lowTargetBlocked) {
            issues += "This app relies on privileged/system integration. AppCompat can attempt userspace virtualization, but Android roles, signature services or OEM-only binders may still be unavailable."
        }
        if (legacyStorage) {
            issues += "The app uses legacy external-storage behavior. Native mode lets Android apply its platform compatibility behavior; virtual mode redirects supported filesystem calls."
        }
        if (targetSdk in 1..22) {
            issues += "Very old framework behavior detected (target API $targetSdk); service, permission and storage translation may be required."
        } else if (targetSdk in 23..28) {
            issues += "Legacy Android behavior detected (target API $targetSdk); AppCompat will choose the least invasive execution route available."
        }
        if (minSdkBlock) {
            issues += "The APK declares a minimum Android API newer than this host device."
        }
        if (requestedPermissions.any { it == "android.permission.GET_ACCOUNTS" || it == "android.permission.USE_CREDENTIALS" }) {
            issues += "The app depends on legacy account APIs; account-backed sign-in may still depend on services or servers that no longer exist."
        }

        // Native-first avoids needless emulation. If Android blocks only the target
        // SDK floor, use the matching helper process even for system-integrated apps;
        // the attempt is valuable, but the UI keeps the caveat visible.
        val route = when {
            minSdkBlock || deviceHasNo32BitRuntime || deviceHasNo64BitRuntime -> "limited"
            nativeInstallable -> "native"
            virtualCompatible -> "virtual"
            else -> "limited"
        }
        val abiCompatible = when (route) {
            "virtual" -> virtualCompatible
            "native" -> nativeInstallable
            else -> false
        }

        val confidence = when {
            route == "native" && systemIntegrated -> "High — real Android services and roles are preserved"
            route == "native" -> "High — Android can execute this APK directly"
            route == "virtual" && abis.isEmpty() -> "High for self-contained Java/Kotlin apps; service dependencies can still fail"
            route == "virtual" -> "Medium — native/JNI and framework assumptions can still be app-specific"
            else -> "Limited — a platform, CPU, hardware or external dependency is unresolved"
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
            "preferredEngineBits" to preferredEngineBits,
            "dexCount" to dexCount,
            "hasNativeCode" to abis.isNotEmpty(),
            "abiCompatible" to abiCompatible,
            "lowTargetBlocked" to lowTargetBlocked,
            "systemIntegrated" to systemIntegrated,
            "nativeInstallable" to nativeInstallable,
            "route" to route,
            "confidence" to confidence,
            "issues" to issues,
            "permissionCount" to requestedPermissions.size
        )
    }
}
