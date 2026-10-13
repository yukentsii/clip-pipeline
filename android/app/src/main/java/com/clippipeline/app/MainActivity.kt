package com.clippipeline.app

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.core.content.IntentCompat
import kotlinx.coroutines.flow.update
import com.clippipeline.app.ui.MainShell
import com.clippipeline.app.ui.pairing.PairingScreen
import com.clippipeline.app.ui.theme.ClipPipelineTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val dark = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT)
        enableEdgeToEdge(statusBarStyle = dark, navigationBarStyle = dark)
        val app = application as ClipApp
        if (savedInstanceState == null) takeShared(intent)
        setContent {
            ClipPipelineTheme {
                val server by app.session.server.collectAsState()
                if (server == null) PairingScreen(app) else MainShell(app)
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        takeShared(intent)
    }

    /** A video or a link shared to clip-pipeline from another app: it is added like any other source. */
    private fun takeShared(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND) return
        val intake = (application as ClipApp).intake
        val video = IntentCompat.getParcelableExtra(intent, Intent.EXTRA_STREAM, Uri::class.java)
        val link = intent.getStringExtra(Intent.EXTRA_TEXT)?.let { text -> Regex("https?://\\S+").find(text)?.value }
        when {
            video != null -> intake.upload(video, hint = null)
            link != null -> intake.shared.value = link
            else -> return
        }
        intake.arrivals.update { it + 1 }
    }
}
