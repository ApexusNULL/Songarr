package com.songarr.songarr_app

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle

/**
 * "Share → Songarr" from YouTube, or anything else with a link: Songarr opens and adds the song at
 * that link (lib/ui/add_link.dart), with a playlist to pick for it. Shows nothing itself.
 */
class ShareLinkActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val text = intent?.getStringExtra(Intent.EXTRA_TEXT) ?: ""
        val url = Regex("https?://\\S+").find(text)?.value
        if (url != null) {
            val link = Uri.Builder().scheme("songarr").authority("add").appendQueryParameter("url", url).build().toString()
            startActivity(Intent(this, MainActivity::class.java)
                .putExtra(DownloadsReceiver.OPEN, link)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        }
        finish()
    }
}
