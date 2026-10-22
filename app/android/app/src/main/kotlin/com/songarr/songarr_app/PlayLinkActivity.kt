package com.songarr.songarr_app

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import io.flutter.plugin.common.MethodChannel

/**
 * Receives songarr://play links from other apps (docs/API.md, "Playing from other apps") without
 * showing anything: when Songarr is already running, the link goes straight to the player and the
 * other app stays in front. Otherwise Songarr opens, plays it, and goes back to the other app.
 */
class PlayLinkActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val uri = intent?.data
        if (uri != null && uri.scheme == "songarr" && uri.host == "play") PlayLinks.deliver(applicationContext, uri)
        finish()
    }
}

/** Where links go: the running player (through its channel), or Songarr, opened with the link waiting. */
object PlayLinks {
    @Volatile var channel: MethodChannel? = null
    @Volatile var pending: String? = null

    fun deliver(app: Context, uri: Uri) {
        val ch = channel ?: return open(app, uri.toString())
        // the player is running, so Songarr isn't shown: there's nothing to go back from
        val direct = uri.buildUpon().clearQuery().apply {
            for (k in uri.queryParameterNames) if (k != "back") appendQueryParameter(k, uri.getQueryParameter(k))
        }.build().toString()
        ch.invokeMethod("open", direct, object : MethodChannel.Result {
            override fun success(result: Any?) {}
            override fun error(code: String, message: String?, details: Any?) {}
            override fun notImplemented() = open(app, uri.toString()) // not listening (signed out, still starting)
        })
    }

    private fun open(app: Context, link: String) {
        pending = link
        app.startActivity(Intent(app, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }
}
