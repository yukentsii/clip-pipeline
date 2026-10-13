package com.clippipeline.app.ui

import androidx.annotation.OptIn
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.common.PlaybackException
import androidx.media3.exoplayer.DefaultRenderersFactory
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.mediacodec.MediaCodecUtil
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.compose.PlayerSurface
import androidx.media3.ui.compose.SURFACE_TYPE_TEXTURE_VIEW
import com.clippipeline.app.ClipApp
import com.clippipeline.app.R
import com.clippipeline.app.data.Account
import com.clippipeline.app.data.AccountList
import com.clippipeline.app.data.Clip
import com.clippipeline.app.ui.common.AccountRows
import com.clippipeline.app.ui.common.Choice
import com.clippipeline.app.ui.common.LabeledField
import com.clippipeline.app.ui.common.LoadedContent
import com.clippipeline.app.ui.common.RemoteImage
import com.clippipeline.app.ui.common.Segmented
import com.clippipeline.app.ui.common.TagsEditor
import com.clippipeline.app.ui.common.explain
import com.clippipeline.app.ui.common.rememberLoaded
import com.clippipeline.app.ui.common.scoreColor
import com.clippipeline.app.ui.common.thumbnailPlaceholder
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

/** Figma frame "2 Clip detail": play the clip, fix its text, choose where it goes, approve or reject. */
@Composable
fun ClipDetailScreen(app: ClipApp, target: Overlay.ClipDetail, onBack: () -> Unit, onDecided: () -> Unit) {
    val clip = rememberLoaded(app, target.clipId) { app.api.get<Clip>("/clips/${target.clipId}") }
    val accounts = rememberLoaded(app) { app.api.get<AccountList>("/accounts").accounts }

    Column(Modifier.fillMaxSize().background(Palette.Background).statusBarsPadding().imePadding()) {
        BackHeader("Review", onBack, trailing = "${target.position} of ${target.total}")
        val loadedClip = clip.value
        val loadedAccounts = accounts.value
        if (loadedClip == null || loadedAccounts == null) {
            Box(Modifier.padding(20.dp)) { LoadedContent(if (loadedClip == null) clip else accounts) {} }
        } else {
            ClipEditor(app, loadedClip, loadedAccounts, onDecided)
        }
    }
}

@Composable
private fun androidx.compose.foundation.layout.ColumnScope.ClipEditor(app: ClipApp, first: Clip, accounts: List<Account>, onDecided: () -> Unit) {
    val scope = rememberCoroutineScope()
    var clip by remember { mutableStateOf(first) }
    var title by remember { mutableStateOf(first.title) }
    var description by remember { mutableStateOf(first.description) }
    var tags by remember { mutableStateOf(first.tags) }
    var hookTitle by remember { mutableStateOf(first.hookTitle) }
    var choices by remember {
        mutableStateOf(accounts.associate { it.id to Choice(chosen = it.configured && it.enabled) })
    }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val rendering = clip.status != "review"

    fun patch(body: JsonObject) {
        scope.launch {
            try {
                clip = app.api.patch("/clips/${clip.id}", body)
                error = null
            } catch (e: Exception) {
                error = app.explain(e)
            }
        }
    }

    // A change to the picture re-renders the clip; wait here until it is back in review.
    LaunchedEffect(rendering) {
        while (clip.status != "review" && clip.status != "failed") {
            delay(2000)
            runCatching { clip = app.api.get("/clips/${clip.id}") }
        }
    }

    Column(
        Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState())
            .padding(start = 20.dp, end = 20.dp, top = 8.dp, bottom = 20.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp),
    ) {
        Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
            ClipPlayer(app, clip.videoUrl.takeIf { !rendering }, poster = clip.thumbUrl, note = if (clip.status == "failed") "Rendering failed" else if (rendering) "Re-rendering…" else null)
        }
        ScoreCard(clip)
        LabeledField("Title", title, { title = it.take(100) }) {
            if (it.isNotBlank() && it != clip.title) patch(buildJsonObject { put("title", it) })
        }
        LabeledField("Description", description, { description = it.take(2200) }) {
            if (it != clip.description) patch(buildJsonObject { put("description", it) })
        }
        TagsEditor(tags) {
            tags = it
            patch(buildJsonObject { putJsonArray("tags") { it.forEach(::add) } })
        }
        LabeledField("On-screen title (first 4 seconds)", hookTitle, { hookTitle = it.take(80) }, singleLine = true, enabled = !rendering) {
            if (it != clip.hookTitle) patch(buildJsonObject { put("hook_title", it) })
        }
        Column(verticalArrangement = Arrangement.spacedBy(7.dp)) {
            Text("Framing", style = inter(12, color = Palette.Muted))
            Segmented(listOf("auto" to "Auto", "crop" to "Crop to faces", "fit" to "Full frame"), clip.layout, enabled = !rendering) {
                patch(buildJsonObject { put("layout", it) })
            }
        }
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Post to", style = inter(12, color = Palette.Muted))
            if (accounts.isEmpty()) {
                Text("There are no accounts yet. Add one in Settings.", style = inter(13, lineHeight = 19, color = Palette.Muted))
            } else {
                AccountRows(accounts, choices, enabled = !busy) { choices = it }
                Text(
                    "Times are each account's next free slot. Tap a time to change it.",
                    style = inter(12, lineHeight = 17, color = Palette.Faint),
                )
            }
        }
    }

    Column(Modifier.fillMaxWidth().background(Palette.Background)) {
        Box(Modifier.fillMaxWidth().height(1.dp).background(Palette.Line))
        error?.let {
            Text(it, style = inter(13, lineHeight = 19, color = Palette.Red), modifier = Modifier.padding(start = 20.dp, end = 20.dp, top = 12.dp))
        }
        val chosen = accounts.filter { choices[it.id]?.chosen == true }
        val canApprove = !busy && !rendering && chosen.isNotEmpty() && title.isNotBlank()
        Row(
            Modifier.fillMaxWidth().navigationBarsPadding().padding(start = 20.dp, end = 20.dp, top = 14.dp, bottom = 14.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            val shape = RoundedCornerShape(26.dp)
            Box(
                Modifier.weight(1f).alpha(if (busy) 0.5f else 1f).clip(shape).border(1.dp, Palette.Line, shape).clickable(enabled = !busy) {
                    busy = true
                    scope.launch {
                        try {
                            app.api.send("POST", "/clips/${clip.id}/reject")
                            onDecided()
                        } catch (e: Exception) {
                            error = app.explain(e)
                            busy = false
                        }
                    }
                }.padding(vertical = 15.dp),
                contentAlignment = Alignment.Center,
            ) { Text("Reject", style = inter(15, FontWeight.Medium)) }
            Box(
                Modifier.weight(1f).alpha(if (canApprove) 1f else 0.5f).clip(shape).background(Palette.Text).clickable(enabled = canApprove) {
                    busy = true
                    scope.launch {
                        try {
                            app.api.send("POST", "/clips/${clip.id}/approve", buildJsonObject {
                                putJsonArray("targets") {
                                    for (account in chosen) add(buildJsonObject {
                                        put("account_id", account.id)
                                        choices[account.id]?.at?.let { put("scheduled_at", it.toString()) }
                                    })
                                }
                                put("title", title.trim())
                                put("description", description)
                                putJsonArray("tags") { tags.forEach(::add) }
                            })
                            onDecided()
                        } catch (e: Exception) {
                            error = app.explain(e)
                            busy = false
                        }
                    }
                }.padding(vertical = 15.dp),
                contentAlignment = Alignment.Center,
            ) { Text("Approve", style = inter(15, FontWeight.SemiBold, color = Palette.Background)) }
        }
    }
}

@Composable
private fun ScoreCard(clip: Clip) {
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Palette.Card).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.Bottom) {
                Text(clip.score.toString(), style = inter(34, FontWeight.Bold, lineHeight = 36, color = scoreColor(clip.score)))
                Text("out of 100", style = inter(13, color = Palette.Muted), modifier = Modifier.padding(bottom = 4.dp))
            }
            Text(clipMeta(clip), style = inter(13, color = Palette.Muted))
        }
        val parts = listOf("Hook" to clip.hookScore, "Engagement" to clip.engagementScore, "Value" to clip.valueScore, "Shareable" to clip.shareabilityScore)
        if (parts.any { it.second != null }) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                for ((label, points) in parts) {
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Box(Modifier.fillMaxWidth().height(4.dp).clip(RoundedCornerShape(2.dp)).background(Palette.Track)) {
                            Box(
                                Modifier.fillMaxWidth(((points ?: 0) / 25f).coerceIn(0f, 1f)).height(4.dp)
                                    .clip(RoundedCornerShape(2.dp)).background(Palette.Text)
                            )
                        }
                        Text(label, style = inter(11, color = Palette.Muted), maxLines = 1)
                        Text(if (points == null) "–" else "$points/25", style = inter(12, FontWeight.Medium))
                    }
                }
            }
        }
        if (clip.hookReason.isNotBlank()) Text(clip.hookReason, style = inter(13, lineHeight = 19, color = Palette.Muted))
    }
}

/** The rendered clip, exactly as it will be posted. Streams through the app's one signed HTTP client. */
@OptIn(UnstableApi::class)
@Composable
fun ClipPlayer(app: ClipApp, videoUrl: String?, poster: String? = null, note: String? = null) {
    val context = LocalContext.current
    // Some hardware decoders refuse some clips part-way through. When that happens the clip is tried
    // once more with the phone's software decoders first.
    var softwareFirst by remember { mutableStateOf(false) }
    val player = remember(softwareFirst) {
        val renderers = DefaultRenderersFactory(context).setEnableDecoderFallback(true)
        if (softwareFirst) {
            renderers.setMediaCodecSelector { mimeType, secure, tunneling ->
                MediaCodecUtil.getDecoderInfos(mimeType, secure, tunneling).sortedBy { !it.softwareOnly }
            }
        }
        ExoPlayer.Builder(context, renderers)
            .setMediaSourceFactory(DefaultMediaSourceFactory(OkHttpDataSource.Factory(app.http)))
            .build().apply { repeatMode = Player.REPEAT_MODE_ONE }
    }
    var playing by remember { mutableStateOf(false) }
    var started by remember(videoUrl) { mutableStateOf(false) }
    var failed by remember(videoUrl) { mutableStateOf(false) }
    DisposableEffect(player) {
        val listener = object : Player.Listener {
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                playing = isPlaying
                if (isPlaying) started = true
            }

            override fun onPlayerError(error: PlaybackException) {
                val decoder = error.errorCode in PlaybackException.ERROR_CODE_DECODER_INIT_FAILED..PlaybackException.ERROR_CODE_DECODING_RESOURCES_RECLAIMED
                if (decoder && !softwareFirst) softwareFirst = true else failed = true
            }
        }
        player.addListener(listener)
        onDispose {
            player.removeListener(listener)
            player.release()
        }
    }
    LaunchedEffect(videoUrl, player) {
        if (videoUrl == null) {
            player.stop()
            player.clearMediaItems()
        } else {
            player.setMediaItem(MediaItem.fromUri(app.api.url(videoUrl).toString()))
            player.prepare()
            if (softwareFirst) player.play()           // carrying on after the retry
        }
    }
    Box(
        Modifier.size(220.dp, 392.dp).clip(RoundedCornerShape(20.dp)).background(thumbnailPlaceholder)
            .clickable(enabled = videoUrl != null, interactionSource = remember { MutableInteractionSource() }, indication = null) {
                if (player.isPlaying) player.pause() else player.play()
            },
        contentAlignment = Alignment.Center,
    ) {
        if (videoUrl != null) PlayerSurface(player, Modifier.fillMaxSize(), surfaceType = SURFACE_TYPE_TEXTURE_VIEW)
        if (!started && poster != null) RemoteImage(app, poster, Modifier.fillMaxSize())
        val words = if (failed) "This clip cannot be played here" else note
        when {
            words != null -> Box(Modifier.fillMaxSize().background(Palette.Background.copy(alpha = 0.6f)), contentAlignment = Alignment.Center) {
                Text(words, style = inter(13, FontWeight.Medium))
            }
            !playing -> Box(Modifier.size(56.dp).clip(CircleShape).background(Palette.Text), contentAlignment = Alignment.Center) {
                Icon(painterResource(R.drawable.ic_play_arrow), contentDescription = "Play", tint = Palette.Background, modifier = Modifier.size(28.dp))
            }
        }
    }
}
