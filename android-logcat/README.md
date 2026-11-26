# Android Logcat Companion (prototype)

This companion is an opt-in Android collector for devices you own or administer. It sends batches to your Render service over HTTPS.

## Server setup
1. In Render, add an environment variable named `ANDROID_LOGCAT_API_KEY` with a long random value (at least 32 characters). Keep it private; do not commit it or share it in chat.
2. Deploy the server changes.
3. Open the admin page at `/android-logcat`.

## Android setup
- Open this folder in Android Studio (recent stable version; Android Gradle Plugin 8.6.1, Kotlin 2.0.21).
- Install the app on an Android test device.
- In the app, enter the service URL (for example `https://YOUR-SERVICE.onrender.com`) and the same API key. The key is stored locally on that device; distribute it only to trusted testers.
- Connect the device to a computer with USB debugging enabled, then run:
  `adb shell pm grant com.daved.logcat android.permission.READ_LOGS`
- Verify the grant: `adb shell dumpsys package com.daved.logcat | grep READ_LOGS`
- In the app, review the disclosure and turn collection ON. Turn it OFF to stop collection and clear the in-memory queue.

## Android restrictions
`READ_LOGS` is a signature/privileged permission. Ordinary Play Store apps cannot silently obtain it; the user/admin must grant it using ADB on supported devices. Some OEM builds restrict it further. Without the grant, the app reports that permission is missing and does not pretend to collect system Logcat.

## Privacy
- Collection is OFF by default and starts only after the user enables it.
- The app shows a persistent foreground notification while collecting.
- Logs may contain personal data, identifiers, or secrets. Use only on devices/accounts you own or are authorized to monitor. Avoid collecting from other people's devices without informed consent.
- The server requires the shared API key and explicit consent flag. The endpoint is rate/size bounded per request; protect the key and rotate it if exposed.
