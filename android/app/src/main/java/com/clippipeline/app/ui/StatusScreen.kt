package com.clippipeline.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.clippipeline.app.ClipApp
import com.clippipeline.app.data.Attention
import com.clippipeline.app.data.Event
import com.clippipeline.app.data.EventList
import com.clippipeline.app.data.Source
import com.clippipeline.app.data.Status
import com.clippipeline.app.ui.common.LoadedContent
import com.clippipeline.app.ui.common.Times
import com.clippipeline.app.ui.common.explain
import com.clippipeline.app.ui.common.platformName
import com.clippipeline.app.ui.common.rememberLoaded
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter
import kotlinx.coroutines.launch
import java.time.LocalDate

/** Figma frame "6 Status": what needs attention, what is being worked on, and what happened lately. */
@Composable
fun StatusScreen(app: ClipApp, onBack: () -> Unit, onSettings: () -> Unit) {
    val status = rememberLoaded(app, refreshMillis = 5_000) { app.api.get<Status>("/status") }
    val events = rememberLoaded(app, refreshMillis = 15_000) { app.api.get<EventList>("/events?limit=30").events }

    Column(Modifier.fillMaxSize().background(Palette.Background).statusBarsPadding()) {
        BackHeader("Back", onBack, title = "Status")
        Column(
            Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState()).navigationBarsPadding()
                .padding(start = 20.dp, end = 20.dp, top = 6.dp, bottom = 20.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            LoadedContent(status) { now ->
                if (now.needsAttention.isNotEmpty()) {
                    Label("Needs attention · ${now.needsAttention.size}")
                    for (item in now.needsAttention) AttentionCard(app, item, onSettings, status.reload)
                }
                Label("Working on")
                if (now.workingOn.isEmpty()) {
                    Text(
                        if (now.queuedSources > 0) waiting(now.queuedSources).replaceFirstChar { it.uppercase() } else "Nothing right now.",
                        style = inter(13, lineHeight = 19, color = Palette.Muted),
                    )
                }
                for (source in now.workingOn) WorkingCard(source, now.queuedSources)
            }
            val lately = events.value
            if (!lately.isNullOrEmpty()) {
                Label("Lately")
                Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Palette.Card).padding(horizontal = 14.dp, vertical = 4.dp)) {
                    lately.forEachIndexed { index, event ->
                        if (index > 0) Box(Modifier.fillMaxWidth().height(1.dp).background(Palette.Line))
                        EventRow(event)
                    }
                }
            }
        }
    }
}

@Composable
private fun Label(text: String) = Text(text, style = inter(12, FontWeight.SemiBold, color = Palette.Muted))

private fun waiting(count: Int) = if (count == 1) "1 more video waiting" else "$count more videos waiting"

@Composable
private fun AttentionCard(app: ClipApp, item: Attention, onSettings: () -> Unit, reload: () -> Unit) {
    val scope = rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val shape = RoundedCornerShape(18.dp)
    Column(
        Modifier.fillMaxWidth().clip(shape).background(Palette.Card).border(1.dp, Palette.Red.copy(alpha = 0.35f), shape).padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(6.dp).clip(RoundedCornerShape(3.dp)).background(Palette.Red))
            Text(
                when (item.kind) {
                    "account" -> "${platformName(item.platform.orEmpty())} · ${item.title}"
                    "post" -> "${platformName(item.platform.orEmpty())}${item.account?.let { " · $it" } ?: ""}"
                    else -> item.title.ifBlank { "A video" }
                },
                style = inter(14, FontWeight.SemiBold), maxLines = 1, overflow = TextOverflow.Ellipsis,
            )
        }
        val reason = item.reason.trim().replaceFirstChar { it.uppercase() }.let { if (it.isEmpty() || it.endsWith(".")) it else "$it." }
        Text(
            when (item.kind) {
                "account" -> "$reason Uploads to this account are paused until you log in again on a computer."
                "post" -> "Could not post “${item.title}”. $reason"
                else -> reason.ifBlank { "It failed without saying why." }
            },
            style = inter(13, lineHeight = 19, color = Palette.Muted),
        )
        error?.let { Text(it, style = inter(13, lineHeight = 19, color = Palette.Red)) }
        val retryPath = when (item.kind) {
            "source" -> "/sources/${item.id}/retry"
            "clip" -> "/clips/${item.id}/retry"
            "post" -> "/posts/${item.id}/retry"
            else -> null
        }
        Box(
            Modifier.alpha(if (busy) 0.5f else 1f).clip(RoundedCornerShape(18.dp)).background(Palette.Text).clickable(enabled = !busy) {
                if (retryPath == null) {
                    onSettings()
                } else {
                    busy = true
                    scope.launch {
                        try {
                            app.api.send("POST", retryPath)
                            reload()
                        } catch (e: Exception) {
                            error = app.explain(e)
                        }
                        busy = false
                    }
                }
            }.padding(horizontal = 16.dp, vertical = 9.dp),
        ) { Text(if (retryPath == null) "Details" else "Retry", style = inter(12, FontWeight.SemiBold, color = Palette.Background)) }
    }
}

private val stageLabels = mapOf("audio" to "Audio", "analysis" to "Analysis", "whisper" to "Whisper", "llm" to "Scoring", "slices" to "Cuts", "render" to "Render")

@Composable
private fun WorkingCard(source: Source, queued: Int) {
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Palette.Card).padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(source.title.ifBlank { source.url.ifBlank { "Video" } }, style = inter(14, FontWeight.SemiBold), maxLines = 1, overflow = TextOverflow.Ellipsis)
        val current = source.stages.indexOf(source.stage)
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            source.stages.forEachIndexed { index, stage ->
                val filled = when {
                    index < current -> 1f
                    index == current -> source.stageProgress.toFloat().coerceIn(0f, 1f)
                    else -> 0f
                }
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Box(Modifier.fillMaxWidth().height(4.dp).clip(RoundedCornerShape(2.dp)).background(Palette.Track)) {
                        Box(Modifier.fillMaxWidth(filled).height(4.dp).clip(RoundedCornerShape(2.dp)).background(Palette.Text))
                    }
                    Text(stageLabels[stage] ?: stage, style = inter(10, color = if (index <= current) Palette.Text else Palette.Faint), maxLines = 1)
                }
            }
        }
        val parts = listOfNotNull(source.message.trim().takeIf { it.isNotEmpty() }, waiting(queued).takeIf { queued > 0 })
        if (parts.isNotEmpty()) Text(parts.joinToString(" · "), style = inter(12, lineHeight = 17, color = Palette.Muted))
    }
}

@Composable
private fun EventRow(event: Event) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 13.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
            Text(event.title, style = inter(14, FontWeight.Medium), maxLines = 1, overflow = TextOverflow.Ellipsis)
            if (event.body.isNotBlank()) Text(event.body, style = inter(12, lineHeight = 17, color = Palette.Faint), maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
        Times.parse(event.createdAt)?.let { at ->
            val day = Times.local(at).toLocalDate()
            Text(
                when (day) {
                    LocalDate.now() -> Times.time(at)
                    LocalDate.now().minusDays(1) -> "Yesterday"
                    else -> Times.dayMonth(at)
                },
                style = inter(12, color = Palette.Faint),
            )
        }
    }
}
