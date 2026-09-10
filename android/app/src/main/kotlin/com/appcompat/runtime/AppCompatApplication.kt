package com.appcompat.runtime

import android.app.Application
import android.content.Context
import android.util.Log
import top.niunaijun.blackbox.BlackBoxCore

class AppCompatApplication : Application() {
    override fun attachBaseContext(base: Context) {
        super.attachBaseContext(base)
        try {
            try {
                BlackBoxCore.get().closeCodeInit()
            } catch (t: Throwable) {
                Log.w("AppCompat", "closeCodeInit skipped", t)
            }

            try {
                BlackBoxCore.get().onBeforeMainApplicationAttach(this, base)
            } catch (t: Throwable) {
                Log.w("AppCompat", "before-attach hook skipped", t)
            }

            CompatRuntime.attachBaseContext(base)

            try {
                BlackBoxCore.get().onAfterMainApplicationAttach(this, base)
            } catch (t: Throwable) {
                Log.w("AppCompat", "after-attach hook skipped", t)
            }
        } catch (t: Throwable) {
            Log.e("AppCompat", "Compatibility runtime attach failed", t)
        }
    }

    override fun onCreate() {
        super.onCreate()
        CompatRuntime.create()
    }
}
