package com.songarr.songarr_app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

/**
 * The songs you added, on their way to the server: one quiet notification, with a progress bar,
 * that the server keeps up to date ("Finding it on YouTube", "12 of 547 songs ready", "Your songs
 * are ready"). The updates come as Firebase data messages (songarr/downloads.py), which Firebase
 * hands to every receiver of the app; this one draws them, whether or not the app is running.
 * Tapping it opens Requested songs.
 */
class DownloadsReceiver : BroadcastReceiver() {
    companion object {
        const val CHANNEL = "downloads"
        const val TAG = "songarr-downloads"
        const val OPEN = "songarr.open" // (an extra MainActivity passes on to the app as a link)
        private const val ID = 1
        private const val SEEN = "downloads_at"

        fun createChannel(context: Context) {
            if (Build.VERSION.SDK_INT < 26) return
            val channel = NotificationChannel(CHANNEL, "Song downloads", NotificationManager.IMPORTANCE_LOW).apply {
                description = "How the songs you added are getting on, until they're ready"
            }
            context.getSystemService(NotificationManager::class.java)?.createNotificationChannel(channel)
        }
    }

    override fun onReceive(context: Context, intent: Intent) {
        val x = intent.extras ?: return
        if (x.getString("type") != "downloads") return
        // Messages can arrive out of order (a quiet one held back while the phone slept): keep the newest.
        val at = x.getString("at")?.toLongOrNull() ?: return
        val prefs = context.getSharedPreferences("songarr_downloads", Context.MODE_PRIVATE)
        if (at <= prefs.getLong(SEEN, 0)) return
        prefs.edit().putLong(SEEN, at).commit()

        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        val kind = x.getString("kind")
        if (kind == "clear") {
            nm.cancel(TAG, ID)
            return
        }
        createChannel(context)
        val done = kind == "done"
        val total = x.getString("total")?.toIntOrNull() ?: 0
        val ready = x.getString("ready")?.toIntOrNull() ?: 0
        val open = Intent(context, MainActivity::class.java)
            .putExtra(OPEN, "songarr://requests")
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val tap = PendingIntent.getActivity(context, 7, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        @Suppress("DEPRECATION")
        val builder = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(context, CHANNEL) else Notification.Builder(context)
        builder
            .setSmallIcon(R.drawable.ic_stat_songarr)
            .setColor(context.getColor(R.color.songarr_accent))
            .setContentTitle(x.getString("title"))
            .setContentText(x.getString("body"))
            .setStyle(Notification.BigTextStyle().bigText(x.getString("body")))
            .setContentIntent(tap)
            .setOnlyAlertOnce(true)
            .setAutoCancel(done)
            .setShowWhen(done)
            .setCategory(if (done) Notification.CATEGORY_STATUS else Notification.CATEGORY_PROGRESS)
        if (!done && total > 0) builder.setProgress(total, ready, x.getString("indeterminate") == "1")
        try {
            nm.notify(TAG, ID, builder.build())
        } catch (_: SecurityException) {
            // notifications not allowed: the app's own progress bars still show it
        }
    }
}
