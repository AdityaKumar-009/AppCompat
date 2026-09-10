package com.appcompat.runtime

import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Process
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
        val runtimeIs64Bit = Process.is64Bit()

        // Android 14 blocks target < 23. Android 15+ raises that floor to target < 24.
        val lowTargetBlocked = when {
            hostSdk >= 35 -> targetSdk < 24
            hostSdk >= 34 -> targetSdk < 23
            else -> false
        }
        val minSdkBlock = minSdk > 0 && minSdk > hostSdk
        val deviceHasNo32BitRuntime = has32 && !has64 && Build.SUPPORTED_32_BIT_ABIS.isEmpty()

        // A process has one ABI. The fat AppCompat APK deliberately prefers its 32-bit
        // process on dual-ABI devices because the legacy apps that actually need the
        // virtual runtime are disproportionately 32-bit. Apps that Android can run
        // natively are never forced through the virtual process just because they are old.
        val runtimeBitnessMismatch = when {
            abis.isEmpty() || !nativeCompatible -> false
            runtimeIs64Bit && has32 && !has64 -> true
            !runtimeIs64Bit && has64 && !has32 -> true
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

        val nativeInstallable = !lowTargetBlocked && !minSdkBlock && nativeCompatible && !deviceHasNo32BitRuntime
        val virtualCompatible = !minSdkBlock && nativeCompatible && !deviceHasNo32BitRuntime && !runtimeBitnessMismatch

        val legacyStorage = targetSdk <= 28 && requestedPermissions.any {
            it == "android.permission.READ_EXTERNAL_STORAGE" ||
                it == "android.permission.WRITE_EXTERNAL_STORAGE"
        }

        val issues = mutableListOf<String>()
        if (nativeInstallable && targetSdk <= 28) {
            issues += "Android can still run this APK directly. Native execution is preferred because it preserves real system services, roles, widgets, launchers and account integration."
        }
        if (systemIntegrated && nativeInstallable) {
            issues += "This app depends on system-level integration. AppCompat will use Android's native install path instead of sandboxing it, which avoids common launcher/widget/service crashes."
        }
        if (lowTargetBlocked) {
            issues += "Modern Android blocks normal installation at this target SDK; AppCompat will use the compatibility runtime when the CPU ABI can be hosted safely."
        }
        if (deviceHasNo32BitRuntime) {
            issues += "This APK contains only 32-bit native code, while this device exposes no 32-bit Android runtime. A system-level native bridge/emulator is required; an ordinary app cannot manufacture a missing 32-bit Zygote."
        } else if (!nativeCompatible) {
            issues += "The APK native libraries do not match any CPU ABI exposed by this device."
        } else if (runtimeBitnessMismatch && lowTargetBlocked) {
            val guestBits = if (has32 && !has64) "32-bit" else "64-bit"
            val runtimeBits = if (runtimeIs64Bit) "64-bit" else "32-bit"
            issues += "This blocked APK is $guestBits while AppCompat's virtual process is $runtimeBits. Android cannot change a process ABI after launch, so this specific combination cannot be virtualized inside one package."
        }
        if (systemIntegrated && lowTargetBlocked) {
            issues += "This app needs OS-level roles/services and is below Android's install floor. A virtual container is unlikely to reproduce those privileged integrations reliably."
        }
        if (legacyStorage) {
            issues += "The app uses legacy external-storage behavior. AppCompat isolates virtual storage; native mode lets Android apply its own compatibility layer."
        }
        if (targetSdk in 1..22) {
            issues += "Very old framework behavior detected (target API $targetSdk); service and permission translation may be required."
        } else if (targetSdk in 23..28) {
            issues += "Legacy Android behavior detected (target API $targetSdk); AppCompat will choose native or virtual execution based on what is safest for this APK."
        }
        if (minSdkBlock) {
            issues += "The APK declares a newer minimum Android API than this host device provides."
        }
        if (requestedPermissions.any { it == "android.permission.GET_ACCOUNTS" || it == "android.permission.USE_CREDENTIALS" }) {
            issues += "The app depends on legacy account APIs; account-backed sign-in may require services that no longer exist."
        }

        // Native-first is intentional. Process-level virtualization cannot perfectly
        // emulate launcher roles, app-widget hosts, OEM binders, DRM, Play services or
        // privileged services. Use it only when Android itself refuses the old target.
        val route = when {
            minSdkBlock || !nativeCompatible || deviceHasNo32BitRuntime -> "limited"
            nativeInstallable -> "native"
            systemIntegrated -> "limited"
            virtualCompatible -> "virtual"
            else -> "limited"
        }
        val abiCompatible = if (route == "virtual") virtualCompatible else nativeCompatible && !deviceHasNo32BitRuntime

        val guestBits = when {
            has32 && !has64 -> 32
            has64 && !has32 -> 64
            has32 && has64 -> if (runtimeIs64Bit) 64 else 32
            else -> if (runtimeIs64Bit) 64 else 32
        }

        val confidence = when {
            route == "native" && systemIntegrated -> "High — native Android path avoids virtualized system-service gaps"
            route == "native" -> "High — Android can execute this APK directly"
            route == "virtual" && abis.isEmpty() -> "High for self-contained Java/Kotlin apps"
            route == "virtual" -> "Medium — virtualized native code can still hit device-specific hooks"
            else -> "Limited — unresolved CPU, target-SDK, or privileged system-integration requirement"
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
            "runtimeBits" to if (runtimeIs64Bit) 64 else 32,
            "guestBits" to guestBits,
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
