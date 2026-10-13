package com.clippipeline.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.clippipeline.app.ClipApp
import com.clippipeline.app.data.Clip
import com.clippipeline.app.data.ClipList
import com.clippipeline.app.ui.common.LoadedContent
import com.clippipeline.app.ui.common.RemoteImage
import com.clippipeline.app.ui.common.rememberLoaded
import com.clippipeline.app.ui.common.scoreColor
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter
import kotlin.math.roundToInt

/** Figma frame "1 Review": the clips waiting for a decision, best first. */
@Composable
fun ReviewScreen(app: ClipApp, onOpen: (clip: Clip, position: Int, total: Int) -> Unit) {
    val loaded = rememberLoaded(app, refreshMillis = 15_000) { app.api.get<ClipList>("/clips").clips }
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 20.dp, end = 20.dp, top = 6.dp, bottom = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        val clips = loaded.value
        if (clips == null) {
            item { LoadedContent(loaded) {} }
        } else {
            item {
                Text(
                    when (clips.size) {
                        0 -> "No clips waiting. Add a video and its clips will show up here."
                        1 -> "1 clip waiting"
                        else -> "${clips.size} clips waiting · best first"
                    },
                    style = inter(13, lineHeight = 19, color = Palette.Muted),
                )
            }
            itemsIndexed(clips, key = { _, clip -> clip.id }) { index, clip ->
                ClipCard(app, clip) { onOpen(clip, index + 1, clips.size) }
            }
        }
    }
}

@Composable
private fun ClipCard(app: ClipApp, clip: Clip, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Palette.Card).clickable(onClick = onClick).padding(12.dp),
        horizontalArrangement = Arrangement.spacedBy(14.dp), verticalAlignment = Alignment.CenterVertically,
    ) {
        RemoteImage(app, clip.thumbUrl, Modifier.size(64.dp, 112.dp).clip(RoundedCornerShape(10.dp)))
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(clip.title, style = inter(15, FontWeight.SemiBold, lineHeight = 20))
            Text(clip.sourceTitle, style = inter(12, color = Palette.Muted), maxLines = 1, overflow = TextOverflow.Ellipsis)
            Row(
                Modifier.padding(top = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    clip.score.toString(), style = inter(13, FontWeight.Bold, color = scoreColor(clip.score)),
                    modifier = Modifier.clip(RoundedCornerShape(12.dp)).background(Palette.Track).padding(horizontal = 9.dp, vertical = 4.dp),
                )
                Text(clipMeta(clip), style = inter(12, color = Palette.Faint))
            }
        }
    }
}

/** "43 s · film" */
fun clipMeta(clip: Clip): String = "${clip.duration.roundToInt()} s · ${clip.contentType}"
