package com.clippipeline.app.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.clippipeline.app.ClipApp
import com.clippipeline.app.data.Insights
import com.clippipeline.app.data.InsightsGroup
import com.clippipeline.app.ui.common.LoadedContent
import com.clippipeline.app.ui.common.Segmented
import com.clippipeline.app.ui.common.platformName
import com.clippipeline.app.ui.common.rememberLoaded
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlin.math.abs
import kotlin.math.roundToInt

/** Figma frame "5 Insights": reach over time, the best clips, and what the well-watched clips have in common. */
@Composable
fun InsightsScreen(app: ClipApp) {
    var days by rememberSaveable { mutableIntStateOf(30) }
    val loaded = rememberLoaded(app, days) { app.api.get<Insights>("/insights?days=$days") }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(start = 20.dp, end = 20.dp, top = 6.dp, bottom = 20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Segmented(listOf("7" to "7 days", "30" to "30 days", "90" to "90 days"), days.toString()) { days = it.toInt() }
        LoadedContent(loaded) { data ->
            if (data.totals.posts == 0) {
                Text("Nothing has been posted yet. The numbers fill in once clips are out.", style = inter(13, lineHeight = 19, color = Palette.Muted))
            }
            ViewsCard(data)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Stat("Likes", grouped(data.totals.likes), Modifier.weight(1f))
                Stat("Comments", grouped(data.totals.comments), Modifier.weight(1f))
                val gained = data.followers.sumOf { it.change }
                Stat("Followers", if (data.followers.isEmpty()) "–" else (if (gained >= 0) "+" else "−") + grouped(abs(gained)), Modifier.weight(1f))
            }

            if (data.topClips.isNotEmpty()) {
                Label("Best clips")
                Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Palette.Card).padding(horizontal = 14.dp, vertical = 4.dp)) {
                    data.topClips.take(5).forEachIndexed { index, clip ->
                        if (index > 0) Box(Modifier.fillMaxWidth().height(1.dp).background(Palette.Line))
                        Row(
                            Modifier.fillMaxWidth().padding(vertical = 12.dp),
                            horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                                Text(clip.title, style = inter(13, FontWeight.Medium, lineHeight = 18))
                                Text(platformName(clip.platform) + (clip.account?.let { " · $it" } ?: ""), style = inter(12, color = Palette.Faint))
                            }
                            Text(clip.views?.let(::compact) ?: "–", style = inter(15, FontWeight.SemiBold))
                        }
                    }
                }
            }

            val breakdowns = listOf(
                "AI score" to data.whatWorks.aiScore.sortedByDescending { it.key },
                "Length" to data.whatWorks.length.sortedBy { lengthOrder.indexOf(it.key) },
                "Posting hour" to data.whatWorks.postingHour.sortedBy { it.key },
            ).filter { (_, rows) -> rows.any { it.avgViews != null } }
            if (breakdowns.isNotEmpty()) {
                Label("What works · average views per clip")
                for ((title, rows) in breakdowns) Breakdown(title, rows)
            }

            val notes = data.notMeasured.map { it.note.trim() }.filter { it.isNotEmpty() }.distinct()
            if (notes.isNotEmpty()) Text(notes.joinToString(" "), style = inter(12, lineHeight = 17, color = Palette.Faint))
        }
    }
}

private val lengthOrder = listOf("under 30 s", "30-45 s", "45-60 s", "over 60 s")

@Composable
private fun Label(text: String) = Text(text, style = inter(12, FontWeight.SemiBold, color = Palette.Muted))

@Composable
private fun ViewsCard(data: Insights) {
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(20.dp)).background(Palette.Card).padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text("Views", style = inter(12, color = Palette.Muted))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.Bottom) {
            Text(compact(data.totals.views), style = inter(40, FontWeight.Bold, lineHeight = 42))
            val change = data.trend.changePct
            val (words, color) = when {
                data.trend.direction == "up" -> "▲ ${change?.let { "${abs(it).roundToInt()}% " } ?: ""}this week" to Palette.Green
                data.trend.direction == "down" -> "▼ ${change?.let { "${abs(it).roundToInt()}% " } ?: ""}this week" to Palette.Red
                else -> null to Palette.Muted
            }
            if (words != null) {
                Text(
                    words, style = inter(12, FontWeight.SemiBold, color = color),
                    modifier = Modifier.padding(bottom = 6.dp).clip(RoundedCornerShape(12.dp)).background(color.copy(alpha = 0.14f))
                        .padding(horizontal = 9.dp, vertical = 4.dp),
                )
            }
        }
        val points = data.timeline.map { it.views.toFloat() }
        Canvas(Modifier.fillMaxWidth().height(120.dp)) {
            if (points.size < 2) return@Canvas
            val top = points.max().coerceAtLeast(1f)
            val inset = 2.dp.toPx()
            fun at(index: Int) = Offset(
                x = size.width * index / (points.size - 1),
                y = inset + (size.height - 2 * inset) * (1f - points[index] / top),
            )
            val line = Path().apply {
                moveTo(at(0).x, at(0).y)
                for (i in 1 until points.size) lineTo(at(i).x, at(i).y)
            }
            val area = Path().apply {
                addPath(line)
                lineTo(size.width, size.height)
                lineTo(0f, size.height)
                close()
            }
            drawPath(area, Brush.verticalGradient(listOf(Palette.Text.copy(alpha = 0.10f), Palette.Text.copy(alpha = 0.02f))))
            drawPath(line, Palette.Text, style = Stroke(width = 2.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round))
        }
        val dates = data.timeline.mapNotNull { runCatching { LocalDate.parse(it.date) }.getOrNull() }
        if (dates.size >= 3) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                for (date in listOf(dates.first(), dates[dates.size / 2], dates.last())) Text(axisDate.format(date), style = inter(11, color = Palette.Faint))
            }
        }
    }
}

private val axisDate = DateTimeFormatter.ofPattern("d MMM", Locale.ENGLISH)

@Composable
private fun Stat(label: String, value: String, modifier: Modifier) {
    Column(modifier.clip(RoundedCornerShape(16.dp)).background(Palette.Card).padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(label, style = inter(12, color = Palette.Muted), maxLines = 1)
        Text(value, style = inter(19, FontWeight.SemiBold), maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
private fun Breakdown(title: String, rows: List<InsightsGroup>) {
    val best = rows.maxOf { it.avgViews ?: 0 }.coerceAtLeast(1)
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Palette.Card).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(title, style = inter(13, FontWeight.Medium))
        for (row in rows) {
            val average = row.avgViews
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(bucketName(row.key), style = inter(12, color = Palette.Muted), modifier = Modifier.width(92.dp), maxLines = 1)
                Box(Modifier.weight(1f).height(6.dp)) {
                    if (average != null && average > 0) {
                        Box(
                            Modifier.fillMaxWidth((average.toFloat() / best).coerceIn(0.02f, 1f)).height(6.dp).clip(RoundedCornerShape(3.dp))
                                .background(if (average == best) Palette.Text else Palette.Faint)
                        )
                    }
                }
                Text(average?.let(::compact) ?: "–", style = inter(12, FontWeight.Medium))
            }
        }
    }
}

/** "under 30 s" → "Under 30 s", "30-45 s" → "30–45 s" */
private fun bucketName(key: String?): String = key.orEmpty().replace("-", "–").replaceFirstChar { it.uppercase() }

/** 48213 → "48.2K" */
fun compact(n: Int): String = when {
    n >= 1_000_000 -> "%.1fM".format(Locale.ROOT, n / 1_000_000.0)
    n >= 1_000 -> "%.1fK".format(Locale.ROOT, n / 1_000.0)
    else -> n.toString()
}

/** 3910 → "3,910" */
fun grouped(n: Int): String = "%,d".format(Locale.ROOT, n)
