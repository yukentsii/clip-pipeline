package com.clippipeline.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.clippipeline.app.ClipApp
import com.clippipeline.app.data.Post
import com.clippipeline.app.data.PostList
import com.clippipeline.app.data.Target
import com.clippipeline.app.ui.common.LoadedContent
import com.clippipeline.app.ui.common.RemoteImage
import com.clippipeline.app.ui.common.Segmented
import com.clippipeline.app.ui.common.Times
import com.clippipeline.app.ui.common.platformName
import com.clippipeline.app.ui.common.rememberLoaded
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter
import java.time.Instant
import java.time.LocalDate

/** Figma frame "4 Schedule": what is going out and when, and what went out. */
@Composable
fun ScheduleScreen(app: ClipApp, onOpen: (Post) -> Unit) {
    var showing by rememberSaveable { mutableStateOf("upcoming") }
    val loaded = rememberLoaded(app, refreshMillis = 15_000) { app.api.get<PostList>("/posts?limit=200").posts }

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 20.dp, end = 20.dp, top = 6.dp, bottom = 16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item { Segmented(listOf("upcoming" to "Upcoming", "posted" to "Posted"), showing) { showing = it } }
        val posts = loaded.value
        if (posts == null) {
            item { LoadedContent(loaded) {} }
            return@LazyColumn
        }
        val upcoming = showing == "upcoming"
        // A post is upcoming while anything of it has still to go out (or failed and can be retried).
        val shown = posts.filter { post -> post.targets.any { it.status != "completed" } == upcoming && post.targets.isNotEmpty() }
        val byDay = shown.groupBy { day(it, upcoming) }.toSortedMap(if (upcoming) naturalOrder<LocalDate>() else reverseOrder<LocalDate>())
        if (shown.isEmpty()) {
            item {
                Text(
                    if (upcoming) "Nothing is scheduled. Clips you approve show up here." else "Nothing has been posted yet.",
                    style = inter(13, lineHeight = 19, color = Palette.Muted),
                )
            }
        }
        for ((date, dayPosts) in byDay) {
            item(key = "day-$date") { Text(Times.dayHeading(date), style = inter(12, FontWeight.SemiBold, color = Palette.Muted)) }
            items(dayPosts.sortedBy { moment(it, upcoming) }, key = { it.id }) { post -> PostCard(app, post, upcoming) { onOpen(post) } }
        }
    }
}

/** When a post counts: its next target still to go out, or (once posted) the last one that went. */
private fun moment(post: Post, upcoming: Boolean): Instant {
    val times = if (upcoming) {
        post.targets.filter { it.status != "completed" }.mapNotNull { Times.parse(it.scheduledAt) }
    } else {
        post.targets.mapNotNull { Times.parse(it.postedAt ?: it.scheduledAt) }
    }
    return (if (upcoming) times.minOrNull() else times.maxOrNull()) ?: Instant.EPOCH
}

private fun day(post: Post, upcoming: Boolean): LocalDate = Times.local(moment(post, upcoming)).toLocalDate()

@Composable
private fun PostCard(app: ClipApp, post: Post, upcoming: Boolean, onClick: () -> Unit) {
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Palette.Card)
            .then(if (upcoming) Modifier.clickable(onClick = onClick) else Modifier).padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
            RemoteImage(app, post.clipId?.let { "/clips/$it/thumb" }, Modifier.size(40.dp, 56.dp).clip(RoundedCornerShape(8.dp)))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(post.title, style = inter(14, FontWeight.SemiBold, lineHeight = 19))
                if (upcoming) Text("Tap to edit text, time or accounts", style = inter(12, color = Palette.Faint))
            }
        }
        for (target in post.targets.sortedBy { it.scheduledAt }) TargetRow(target)
    }
}

@Composable
fun TargetRow(target: Target) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
        Row(Modifier.weight(1f, fill = false).padding(end = 12.dp), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(
                Modifier.size(6.dp).clip(RoundedCornerShape(3.dp)).background(
                    when (target.status) {
                        "completed" -> Palette.Green
                        "failed" -> Palette.Red
                        else -> Palette.Faint
                    }
                )
            )
            Text(
                platformName(target.platform) + (target.account?.let { " · $it" } ?: ""),
                style = inter(13), maxLines = 1, overflow = TextOverflow.Ellipsis,
            )
        }
        val at = Times.parse(target.scheduledAt)
        when (target.status) {
            "completed" -> Text("Posted " + (Times.parse(target.postedAt) ?: at)?.let(Times::time).orEmpty(), style = inter(13, color = Palette.Muted))
            "failed" -> Text("Failed · ${shortReason(target.error)}", style = inter(13, color = Palette.Red), maxLines = 1)
            "processing" -> Text("Posting now", style = inter(13, color = Palette.Muted))
            else -> Text(at?.let(Times::time) ?: "", style = inter(13, color = Palette.Muted))
        }
    }
}

/** The first few words of a failure, for a line with little room. The full text is on the Status screen. */
fun shortReason(error: String?): String {
    val first = error.orEmpty().lineSequence().firstOrNull { it.isNotBlank() }?.trim().orEmpty()
    val words = first.substringBefore(". ").substringBefore(": ").ifBlank { "see Status" }.replaceFirstChar { it.lowercase() }
    return if (words.length > 26) words.take(25).trimEnd() + "…" else words
}
