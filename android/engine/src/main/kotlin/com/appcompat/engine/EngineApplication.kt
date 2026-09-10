package com.appcompat.engine

import android.app.Application
import android.content.Context
import android.util.Log
import top.niunaijun.blackbox.BlackBoxCore

class EngineApplication : Application() {
    override fun attachBaseContext(base: Context) {
        super.attachBaseContext(base)
        try {
            try {
                BlackBoxCore.get().closeCodeInit()
            } catch (t: Throwable) {
                Log.w("AppCompatEngine", "closeCodeInit skipped", t)
            }

            try {
                BlackBoxCore.get().onBeforeMainApplicationAttach(this, base)
            } catch (t: Throwable) {
                Log.w("AppCompatEngine", "before-attach hook skipped", t)
            }

            EngineRuntime.attachBaseContext(base)

            try {
                BlackBoxCore.get().onAfterMainApplicationAttach(this, base)
            } catch (t: Throwable) {
                Log.w("AppCompatEngine", "after-attach hook skipped", t)
            }
        } catch (t: Throwable) {
            Log.e("AppCompatEngine", "Compatibility runtime attach failed", t)
        }
    }

    override fun onCreate() {
        super.onCreate()
        EngineRuntime.create(this)
    }
}
