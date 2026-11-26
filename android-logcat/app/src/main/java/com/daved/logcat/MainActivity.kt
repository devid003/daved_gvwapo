package com.daved.logcat

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.*

class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences("config", MODE_PRIVATE)
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(24, 32, 24, 24) }
        root.addView(TextView(this).apply { text = "Daved Android Logcat"; textSize = 24f })
        root.addView(TextView(this).apply { text = "Collection is OFF by default. Only enable this on a device you own or are authorized to monitor. Logs may contain personal information." })
        val endpoint = EditText(this).apply { hint = "Server URL (https://your-service.onrender.com)"; setText(prefs.getString("endpoint", "")) }
        val key = EditText(this).apply { hint = "ANDROID_LOGCAT_API_KEY"; setText(prefs.getString("key", "")) }
        root.addView(endpoint); root.addView(key)
        val status = TextView(this).apply { text = "Status: stopped" }; root.addView(status)
        val start = Button(this).apply { text = "I CONSENT — START LOG COLLECTION" }
        val stop = Button(this).apply { text = "STOP COLLECTION" }
        root.addView(start); root.addView(stop)
        start.setOnClickListener {
            val url = endpoint.text.toString().trim().trimEnd('/')
            val secret = key.text.toString().trim()
            if (!url.startsWith("https://") || secret.length < 32) { status.text = "Enter HTTPS server URL and API key (32+ chars)."; return@setOnClickListener }
            if (checkSelfPermission("android.permission.READ_LOGS") != PackageManager.PERMISSION_GRANTED) {
                status.text = "READ_LOGS not granted. Connect USB debugging and run the ADB command in README."
                return@setOnClickListener
            }
            prefs.edit().putString("endpoint", url).putString("key", secret).apply()
            val i = Intent(this, LogcatService::class.java).putExtra("endpoint", url).putExtra("key", secret)
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(i) else startService(i)
            status.text = "Status: collecting (foreground notification active)"
        }
        stop.setOnClickListener { stopService(Intent(this, LogcatService::class.java)); status.text = "Status: stopped" }
        setContentView(root)
    }
}
