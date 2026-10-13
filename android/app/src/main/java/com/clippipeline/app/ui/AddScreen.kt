package com.clippipeline.app.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.clippipeline.app.ClipApp
import com.clippipeline.app.data.Intake
import com.clippipeline.app.data.Source
import com.clippipeline.app.data.SourceList
import com.clippipeline.app.ui.common.Chip
import com.clippipeline.app.ui.common.OutlineButton
import com.clippipeline.app.ui.common.explain
import com.clippipeline.app.ui.common.rememberLoaded
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter
import kotlinx.coroutines.launch

private val kinds = listOf(null to "Detect", "film" to "Film", "podcast" to "Podcast", "gaming" to "Gaming",
    "music" to "Music", "sports" to "Sports", "vlog" to "Vlog", "education" to "Education")

/** Figma frame "3 Add": give the service a link or a video and watch it being worked on. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun AddScreen(app: ClipApp) {
    val scope = rememberCoroutineScope()
    val intake = app.intake
    val added by intake.added.collectAsState()
    val uploads by intake.uploads.collectAsState()
    val shared by intake.shared.collectAsState()
    val sources = rememberLoaded(app, added, refreshMillis = 4_000) { app.api.get<SourceList>("/sources").sources }

    var link by rememberSaveable { mutableStateOf("") }
    var kind by rememberSaveable { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var note by remember { mutableStateOf<Pair<String, Boolean>?>(null) }      // text, is it an error
    val pickVideo = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri != null) intake.upload(uri, kind)
    }

    fun addLink(url: String) {
        if (url.isBlank() || busy) return
        busy = true
        note = null
        scope.launch {
            try {
                note = intake.addLink(url, kind)?.let { it to false }
                link = ""
            } catch (e: Exception) {
                note = app.explain(e) to true
            }
            busy = false
        }
    }

    // A link shared to the app from elsewhere is added straight away, like any other source.
    LaunchedEffect(shared) {
        shared?.let {
            intake.shared.value = null
            addLink(it)
        }
    }

    Column(
        Modifier.fillMaxSize().imePadding().verticalScroll(rememberScrollState())
            .padding(start = 20.dp, end = 20.dp, top = 6.dp, bottom = 16.dp),
        verticalArrangement = Arrangement.spacedBy(18.dp),
    ) {
        Text("Give it a video. It starts by itself and tells you when clips are ready.", style = inter(13, lineHeight = 19, color = Palette.Muted))

        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            val pill = RoundedCornerShape(28.dp)
            Row(
                Modifier.fillMaxWidth().clip(pill).background(Palette.Card).border(1.dp, Palette.Line, pill)
                    .padding(start = 18.dp, top = 6.dp, end = 6.dp, bottom = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically,
            ) {
                BasicTextField(
                    value = link, onValueChange = { link = it }, singleLine = true, enabled = !busy,
                    textStyle = inter(15), cursorBrush = SolidColor(Palette.Text),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, autoCorrectEnabled = false),
                    modifier = Modifier.weight(1f),
                    decorationBox = { inner ->
                        Box {
                            if (link.isEmpty()) Text("Paste a link", style = inter(15, color = Palette.Faint))
                            inner()
                        }
                    },
                )
                Box(
                    Modifier.alpha(if (busy) 0.5f else 1f).clip(RoundedCornerShape(22.dp)).background(Palette.Text)
                        .clickable(enabled = !busy) { addLink(link) }.padding(horizontal = 22.dp, vertical = 12.dp),
                ) { Text("Add", style = inter(14, FontWeight.SemiBold, color = Palette.Background)) }
            }
            note?.let { (text, isError) ->
                Text(text, style = inter(12, lineHeight = 17, color = if (isError) Palette.Red else Palette.Muted))
            }
        }

        val dashed = RoundedCornerShape(18.dp)
        Column(
            Modifier.fillMaxWidth().clip(dashed).border(1.dp, Palette.Faint, dashed).clickable { pickVideo.launch("video/*") }
                .padding(horizontal = 16.dp, vertical = 26.dp),
            horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Text("Choose a video from this phone", style = inter(15, FontWeight.Medium))
            Text("or share one to clip-pipeline from any app", style = inter(12, color = Palette.Muted))
        }

        Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
            Text("What is it?", style = inter(12, FontWeight.SemiBold, color = Palette.Muted))
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(9.dp)) {
                for ((value, label) in kinds) Chip(label, selected = kind == value) { kind = value }
            }
        }

        val all = sources.value
        val working = all?.filter { it.status != "ready" }.orEmpty()
        val done = all?.filter { it.status == "ready" }.orEmpty()
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (uploads.isNotEmpty() || working.isNotEmpty()) {
                Text("In progress", style = inter(12, FontWeight.SemiBold, color = Palette.Muted))
                for (upload in uploads) UploadCard(upload, onRetry = { intake.retry(upload.id) }, onDismiss = { intake.dismiss(upload.id) })
                for (source in working) WorkingCard(source)
            }
            if (done.isNotEmpty()) {
                Text("Done", style = inter(12, FontWeight.SemiBold, color = Palette.Muted))
                for (source in done) DoneCard(source)
            }
            if (all == null && sources.error != null) {
                Text(sources.error, style = inter(13, lineHeight = 19, color = Palette.Red))
            }
        }
    }
}

@Composable
private fun ProgressCard(title: String, line: String, lineColor: androidx.compose.ui.graphics.Color, progress: Float, onClick: (() -> Unit)? = null) {
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(Palette.Card)
            .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier).padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Text(title, style = inter(14, FontWeight.Medium), maxLines = 1, overflow = TextOverflow.Ellipsis)
        Text(line, style = inter(12, lineHeight = 17, color = lineColor))
        Box(Modifier.fillMaxWidth().height(4.dp).clip(RoundedCornerShape(2.dp)).background(Palette.Track)) {
            Box(Modifier.fillMaxWidth(progress.coerceIn(0f, 1f)).height(4.dp).clip(RoundedCornerShape(2.dp)).background(Palette.Text))
        }
    }
}

@Composable
private fun UploadCard(upload: Intake.Upload, onRetry: () -> Unit, onDismiss: () -> Unit) {
    val percent = (upload.progress * 100).toInt().coerceIn(0, 100)
    when {
        upload.error != null -> Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            ProgressCard(upload.name, "Upload failed · ${upload.error.trimEnd('.')}. Tap to try again.", Palette.Red, upload.progress, onRetry)
            OutlineButton("Dismiss", onClick = onDismiss)
        }
        upload.reading -> ProgressCard(upload.name, "Getting ready to upload · $percent%", Palette.Muted, 0f)
        else -> ProgressCard(upload.name, "Uploading · $percent%", Palette.Muted, upload.progress)
    }
}

private val stageNames = mapOf(
    "audio" to "Getting the audio", "analysis" to "Finding the best moments", "whisper" to "Transcribing",
    "llm" to "Scoring", "slices" to "Cutting clips", "render" to "Rendering",
)

@Composable
private fun WorkingCard(source: Source) {
    val title = source.title.ifBlank { source.url.ifBlank { "Video" } }
    val steps = source.stages.size.coerceAtLeast(1)
    val step = source.stages.indexOf(source.stage).coerceAtLeast(0)
    when (source.status) {
        "failed" -> ProgressCard(title, "Failed · open the bell at the top to see why and try again", Palette.Red, (step + source.stageProgress.toFloat()) / steps)
        "queued" -> ProgressCard(title, "Waiting in line", Palette.Muted, 0f)
        else -> ProgressCard(
            title, "${stageNames[source.stage] ?: source.stage} · step ${step + 1} of $steps", Palette.Muted,
            (step + source.stageProgress.toFloat()) / steps,
        )
    }
}

@Composable
private fun DoneCard(source: Source) {
    val waiting = source.clips["review"] ?: 0
    val total = source.clips.filterKeys { it != "below_threshold" && it != "duplicate" }.values.sum()
    Row(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(Palette.Card).padding(14.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(source.title.ifBlank { source.url }, style = inter(14, FontWeight.Medium), maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        when {
            waiting > 0 -> Text(if (waiting == 1) "1 clip ready" else "$waiting clips ready", style = inter(12, color = Palette.Green))
            total > 0 -> Text("All clips decided", style = inter(12, color = Palette.Muted))
            else -> Text("No clips made", style = inter(12, color = Palette.Muted))
        }
    }
}
