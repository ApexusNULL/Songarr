package com.songarr.songarr_app

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import com.ryanheise.audioservice.AudioServiceActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.security.MessageDigest
import kotlin.concurrent.thread

// AudioServiceActivity keeps playback running in the background with media controls.
class MainActivity : AudioServiceActivity() {
    /** "Jam invites": high importance, so an invite pops up even when the app is closed. */
    private fun createJamChannel() {
        if (Build.VERSION.SDK_INT < 26) return
        val channel = NotificationChannel("jams", "Jam invites", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "When someone in your family starts a Jam with you"
        }
        val releases = NotificationChannel("releases", "New releases", NotificationManager.IMPORTANCE_DEFAULT).apply {
            description = "When an artist you follow puts out something new"
        }
        getSystemService(NotificationManager::class.java)?.createNotificationChannels(listOf(channel, releases))
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        createJamChannel()
        val messenger = flutterEngine.dartExecutor.binaryMessenger
        // Android 13+ only shows the playback notification (and lock-screen player) once the
        // app may post notifications, so ask the first time.
        MethodChannel(messenger, "songarr/permissions").setMethodCallHandler { call, result ->
            if (call.method == "requestNotifications") {
                if (Build.VERSION.SDK_INT >= 33 &&
                    checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
                ) {
                    requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 7)
                }
                result.success(null)
            } else {
                result.notImplemented()
            }
        }
        // The app's own icon: a rebuilt app carries the one chosen on the server (shown before signing in).
        MethodChannel(messenger, "songarr/brand").setMethodCallHandler { call, result ->
            if (call.method == "appIcon") {
                val size = 256
                val bitmap = android.graphics.Bitmap.createBitmap(size, size, android.graphics.Bitmap.Config.ARGB_8888)
                packageManager.getApplicationIcon(packageName).apply { setBounds(0, 0, size, size) }
                    .draw(android.graphics.Canvas(bitmap))
                val png = java.io.ByteArrayOutputStream()
                bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, png)
                result.success(png.toByteArray())
            } else {
                result.notImplemented()
            }
        }
        // Updates from the Songarr server: which build this is, and installing a newer one.
        MethodChannel(messenger, "songarr/update").setMethodCallHandler { call, result ->
            when (call.method) {
                "info" -> {
                    val info = packageManager.getPackageInfo(packageName, 0)
                    val code = if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else @Suppress("DEPRECATION") info.versionCode.toLong()
                    result.success(mapOf("versionCode" to code, "versionName" to info.versionName, "abi" to (Build.SUPPORTED_ABIS.firstOrNull() ?: "")))
                }
                "install" -> {
                    val path = call.argument<String>("path")
                    val sha256 = call.argument<String>("sha256")
                    if (path == null) {
                        result.error("install", "no file", null)
                    } else {
                        thread {
                            val outcome = runCatching { installApk(File(path), sha256) }
                            runOnUiThread {
                                outcome.fold({ result.success(it) }, { result.error("install", it.message, null) })
                            }
                        }
                    }
                }
                else -> result.notImplemented()
            }
        }
    }

    /** Hands the APK to Android's package installer. Returns "started" or "needs-permission". */
    private fun installApk(file: File, sha256: String?): String {
        if (sha256 != null && sha256Of(file) != sha256.lowercase()) {
            file.delete()
            throw IllegalStateException("The download was damaged; it will be fetched again.")
        }
        if (Build.VERSION.SDK_INT >= 26 && !packageManager.canRequestPackageInstalls()) {
            // Once only: the user lets Songarr install its own updates.
            startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
            return "needs-permission"
        }
        val installer = packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
        params.setAppPackageName(packageName)
        if (Build.VERSION.SDK_INT >= 31) {
            // After Songarr has installed itself once, later updates can go in without a prompt.
            params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        }
        val id = installer.createSession(params)
        installer.openSession(id).use { session ->
            file.inputStream().use { input ->
                session.openWrite("songarr.apk", 0, file.length()).use { out ->
                    input.copyTo(out)
                    session.fsync(out)
                }
            }
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
            val status = PendingIntent.getBroadcast(this, id, Intent(this, UpdateReceiver::class.java), flags)
            session.commit(status.intentSender)
        }
        return "started"
    }

    private fun sha256Of(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buf = ByteArray(1 shl 16)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                digest.update(buf, 0, n)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }
}
