package com.songarr.songarr_app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

/** After an update replaces the app (which closes it), offer a tap to reopen it. */
class UpdatedReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(NotificationChannel("com.songarr.app.updates", "App updates", NotificationManager.IMPORTANCE_DEFAULT))
        }
        val version = context.packageManager.getPackageInfo(context.packageName, 0).versionName
        val open = context.packageManager.getLaunchIntentForPackage(context.packageName) ?: return
        val tap = PendingIntent.getActivity(context, 0, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        @Suppress("DEPRECATION")
        val builder = if (Build.VERSION.SDK_INT >= 26) android.app.Notification.Builder(context, "com.songarr.app.updates")
                      else android.app.Notification.Builder(context)
        val note = builder
            .setSmallIcon(R.drawable.ic_stat_songarr)
            .setContentTitle("${context.getString(R.string.app_name)} was updated to $version")
            .setContentText("Tap to open it again.")
            .setContentIntent(tap)
            .setAutoCancel(true)
            .build()
        try {
            nm.notify(4711, note)
        } catch (_: SecurityException) {
            // notifications not allowed: nothing to show
        }
    }
}
