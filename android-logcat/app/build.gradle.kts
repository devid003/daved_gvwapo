plugins { id("com.android.application"); id("org.jetbrains.kotlin.android") }
android {
    namespace = "com.daved.logcat"
    compileSdk = 35
    defaultConfig { applicationId = "com.daved.logcat"; minSdk = 26; targetSdk = 35; versionCode = 1; versionName = "1.0.0" }
}
