package com.appcompat.engine

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.util.Log
import java.util.concurrent.Executors

class EngineBridgeActivity : Activity() {
    companion object {
        const val ACTION_RUN = "com.appcompat.runtime.engine.RUN"
        const val ACTION_LAUNCH = "com.appcompat.runtime.engine.LAUNCH"
        const val ACTION_REMOVE = "com.appcompat.runtime.engine.REMOVE"
        const val ACTION_LAST_CRASH = "com.appcompat.runtime.engine.LAST_CRASH"
    }

    private val worker = Executors.newSingleThreadExecutor()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val action = intent?.action
        val packageName = intent?.getStringExtra("packageName").orEmpty()

        worker.execute {
            val result = try {
                when (action) {
                    ACTION_RUN -> {
                        // The visible host grants only this exact APK through a
                        // FileProvider content URI. No broad storage permission or
                        // path sharing between the two packages is required.
                        val uri = intent?.data?.toString()
                            ?.takeIf { it.isNotBlank() }
                            ?: intent?.getStringExtra("uri").orEmpty()
                        if (packageName.isBlank() || uri.isBlank()) {
                            mapOf("launched" to false, "message" to "APK URI or package name is missing.")
                        } else {
                            EngineRuntime.installAndLaunch(applicationContext, uri, packageName)
                        }
                    }
                    ACTION_LAUNCH -> {
                        if (packageName.isBlank()) {
                            mapOf("launched" to false, "message" to "Package name is missing.")
                        } else {
                            EngineRuntime.launch(packageName)
                        }
                    }
                    ACTION_REMOVE -> {
                        if (packageName.isBlank()) {
                            mapOf("removed" to false, "message" to "Package name is missing.")
                        } else {
                            EngineRuntime.remove(packageName)
                        }
                    }
                    ACTION_LAST_CRASH -> EngineRuntime.lastCrash()
                    else -> mapOf("launched" to false, "message" to "Unknown compatibility engine request.")
                }
            } catch (t: Throwable) {
                Log.e("AppCompatEngine", "Engine bridge request failed", t)
                mapOf("launched" to false, "message" to (t.message ?: t.javaClass.simpleName))
            }

            runOnUiThread {
                val data = Intent()
                result.forEach { (key, value) ->
                    when (value) {
                        is Boolean -> data.putExtra(key, value)
                        is Int -> data.putExtra(key, value)
                        is Long -> data.putExtra(key, value)
                        is String -> data.putExtra(key, value)
                        null -> Unit
                        else -> data.putExtra(key, value.toString())
                    }
                }
                val ok = result["launched"] == true || result["removed"] == true || action == ACTION_LAST_CRASH
                setResult(if (ok) RESULT_OK else RESULT_CANCELED, data)
                finish()
            }
        }
    }

    override fun onDestroy() {
        worker.shutdownNow()
        super.onDestroy()
    }
}
