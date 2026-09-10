package com.appcompat.runtime

import android.app.Application

/**
 * The visible AppCompat process only owns UI, APK inspection and runtime routing.
 * Keeping BlackBox out of this process prevents Android from locking every guest
 * to the UI process' primary ABI and avoids hook side-effects on the Flutter host.
 */
class AppCompatApplication : Application()
