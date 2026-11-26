package com.daved.logcat

import android.app.*
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID
import org.json.JSONArray
import org.json.JSONObject
import kotlin.concurrent.thread

class LogcatService : Service() {
    @Volatile private var running = false
    private var reader: Process? = null
    private val queue = mutableListOf<JSONObject>()
    private val lock = Any()
    private val deviceId by lazy { getSharedPreferences("config", MODE_PRIVATE).getString("device_id", null) ?: UUID.randomUUID().toString().also { getSharedPreferences("config", MODE_PRIVATE).edit().putString("device_id", it).apply() } }

    override fun onBind(intent: Intent?): IBinder? = null
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val endpoint = intent?.getStringExtra("endpoint") ?: return START_NOT_STICKY
        val key = intent.getStringExtra("key") ?: return START_NOT_STICKY
        createChannel()
        val notification = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, "logcat")
            .setContentTitle("Daved Logcat is active").setContentText("Opt-in device diagnostics are being sent to your server.")
            .setSmallIcon(android.R.drawable.ic_menu_info_details).build()
        else Notification.Builder(this).setContentTitle("Daved Logcat is active").setSmallIcon(android.R.drawable.ic_menu_info_details).build()
        startForeground(42, notification)
        if (!running) {
            running = true
            thread(name = "logcat-reader") { collect(endpoint, key) }
        }
        return START_STICKY
    }
    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= 26) getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel("logcat", "Logcat collection", NotificationManager.IMPORTANCE_LOW))
    }
    private fun collect(endpoint: String, key: String) {
        try {
            reader = ProcessBuilder("logcat", "-v", "threadtime").redirectErrorStream(true).start()
            val input = reader!!.inputStream.bufferedReader()
            var lastSend = System.currentTimeMillis()
            while (running) {
                val line = input.readLine() ?: break
                val parsed = parseLine(line)
                synchronized(lock) { if (queue.size < 500) queue.add(parsed) }
                if (System.currentTimeMillis() - lastSend >= 8000) {
                    flush(endpoint, key); lastSend = System.currentTimeMillis()
                }
            }
        } catch (_: Exception) {
            // The user can inspect status and permission instructions in the README.
        } finally { flush(endpoint, key) }
    }
    private fun parseLine(line: String): JSONObject {
        val priority = Regex("\\s([VDIWEFA])\\s").find(line)?.groupValues?.get(1) ?: "I"
        val tag = Regex("\\s[VDIWEFA]\\s+([^(:]+)").find(line)?.groupValues?.get(1)?.trim() ?: "logcat"
        val device = Build.MANUFACTURER + " " + Build.MODEL
        return JSONObject().put("device_id", deviceId).put("device_name", device)
            .put("app_version", "1.0.0").put("priority", priority).put("tag", tag).put("message", line)
    }
    private fun flush(endpoint: String, key: String) {
        val batch = synchronized(lock) { if (queue.isEmpty()) return; JSONArray(queue.toList()).also { queue.clear() } }
        try {
            val conn = URL("$endpoint/api/android-logcat").openConnection() as HttpURLConnection
            conn.requestMethod = "POST"; conn.connectTimeout = 10000; conn.readTimeout = 10000
            conn.doOutput = true; conn.setRequestProperty("Content-Type", "application/json")
            conn.setRequestProperty("X-Logcat-Key", key)
            val body = JSONObject().put("consent", true).put("entries", batch).toString()
            conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            if (code !in 200..299) synchronized(lock) { for (i in 0 until batch.length()) if (queue.size < 500) queue.add(batch.getJSONObject(i)) }
            conn.disconnect()
        } catch (_: Exception) {
            synchronized(lock) { for (i in 0 until batch.length()) if (queue.size < 500) queue.add(batch.getJSONObject(i)) }
        }
    }
    override fun onDestroy() {
        running = false
        reader?.destroy()
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }
}
