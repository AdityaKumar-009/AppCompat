package com.appcompat.runtime

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.util.Log
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.Executors

class MainActivity : FlutterActivity() {
    companion object {
        private const val CHANNEL = "com.appcompat.runtime/bridge"
        private const val REQUEST_APK = 4401
    }

    private val worker = Executors.newSingleThreadExecutor()
    private var pendingPickerResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler(::handleCall)
    }

    private fun handleCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "selectAndAnalyze" -> selectAndAnalyze(result)
            "engineStatus" -> result.success(
                mapOf(
                    "ready" to CompatRuntime.ready,
                    "hostSdk" to android.os.Build.VERSION.SDK_INT,
                    "hostAbis" to android.os.Build.SUPPORTED_ABIS.toList()
                )
            )
            "runVirtual" -> {
                val path = call.argument<String>("path")
                val packageName = call.argument<String>("packageName")
                if (path.isNullOrBlank() || packageName.isNullOrBlank()) {
                    result.error("bad_arguments", "APK path or package name is missing.", null)
                } else {
                    background(result) { CompatRuntime.installAndLaunch(path, packageName) }
                }
            }
            "listVirtualApps" -> background(result) { CompatRuntime.listApps() }
            "launchVirtual" -> {
                val packageName = call.argument<String>("packageName")
                if (packageName.isNullOrBlank()) {
                    result.error("bad_arguments", "Package name is missing.", null)
                } else {
                    background(result) { CompatRuntime.launch(packageName) }
                }
            }
            "removeVirtual" -> {
                val packageName = call.argument<String>("packageName")
                if (packageName.isNullOrBlank()) {
                    result.error("bad_arguments", "Package name is missing.", null)
                } else {
                    background(result) {
                        CompatRuntime.remove(packageName)
                        true
                    }
                }
            }
            "installNormally" -> installNormally(call, result)
            else -> result.notImplemented()
        }
    }

    private fun selectAndAnalyze(result: MethodChannel.Result) {
        if (pendingPickerResult != null) {
            result.error("picker_busy", "The APK picker is already open.", null)
            return
        }
        pendingPickerResult = result
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "application/vnd.android.package-archive"
            putExtra(
                Intent.EXTRA_MIME_TYPES,
                arrayOf("application/vnd.android.package-archive", "application/octet-stream")
            )
        }
        try {
            startActivityForResult(intent, REQUEST_APK)
        } catch (t: Throwable) {
            pendingPickerResult = null
            result.error("picker_unavailable", "No file picker is available on this device.", t.message)
        }
    }

    @Deprecated("Deprecated in Android framework; retained for broad device compatibility")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_APK) return

        val result = pendingPickerResult ?: return
        pendingPickerResult = null
        val uri = data?.data
        if (resultCode != Activity.RESULT_OK || uri == null) {
            result.success(null)
            return
        }

        try {
            val flags = data.flags and
                (Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            contentResolver.takePersistableUriPermission(uri, flags and Intent.FLAG_GRANT_READ_URI_PERMISSION)
        } catch (_: Throwable) {
            // Some providers grant only a transient URI. The analyzer immediately makes a private copy.
        }

        worker.execute {
            try {
                val report = ApkAnalyzer.analyze(applicationContext, uri)
                runOnUiThread { result.success(report) }
            } catch (t: Throwable) {
                Log.e("AppCompat", "APK analysis failed", t)
                runOnUiThread {
                    result.error("analysis_failed", t.message ?: "Could not analyze this APK.", null)
                }
            }
        }
    }

    private fun installNormally(call: MethodCall, result: MethodChannel.Result) {
        val rawUri = call.argument<String>("uri")
        if (rawUri.isNullOrBlank()) {
            result.error("bad_arguments", "Original APK URI is missing.", null)
            return
        }
        try {
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(Uri.parse(rawUri), "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            startActivity(intent)
            result.success(null)
        } catch (t: Throwable) {
            result.error("installer_unavailable", "Android could not open its package installer.", t.message)
        }
    }

    private fun background(result: MethodChannel.Result, block: () -> Any?) {
        worker.execute {
            try {
                val value = block()
                runOnUiThread { result.success(value) }
            } catch (t: Throwable) {
                Log.e("AppCompat", "Native operation failed", t)
                runOnUiThread {
                    result.error("native_failure", t.message ?: t.javaClass.simpleName, null)
                }
            }
        }
    }

    override fun onDestroy() {
        pendingPickerResult = null
        super.onDestroy()
    }
}
