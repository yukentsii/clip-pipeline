package com.clippipeline.app.ui.common

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/** The server speaks UTC ("2026-10-13T07:00:00Z"); the phone shows its own time, day before month. */
object Times {
    private val time = DateTimeFormatter.ofPattern("HH:mm", Locale.ROOT)
    private val dayMonth = DateTimeFormatter.ofPattern("d MMM", Locale.ENGLISH)
    private val weekday = DateTimeFormatter.ofPattern("EEEE d MMM", Locale.ENGLISH)
    private val numeric = DateTimeFormatter.ofPattern("dd/MM/yyyy", Locale.ROOT)

    fun parse(iso: String?): Instant? = iso?.let { runCatching { Instant.parse(it) }.getOrNull() }

    fun local(instant: Instant, zone: ZoneId = ZoneId.systemDefault()): ZonedDateTime = instant.atZone(zone)

    /** "Today 18:00", "Tomorrow 12:30", otherwise "15/10/2026 18:00". */
    fun dayAndTime(instant: Instant, today: LocalDate = LocalDate.now(), zone: ZoneId = ZoneId.systemDefault()): String {
        val at = local(instant, zone)
        return "${dayName(at.toLocalDate(), today) ?: numeric.format(at)} ${time.format(at)}"
    }

    fun time(instant: Instant, zone: ZoneId = ZoneId.systemDefault()): String = time.format(local(instant, zone))

    /** "Today", "Tomorrow", "Yesterday", or null for any other day. */
    fun dayName(day: LocalDate, today: LocalDate = LocalDate.now()): String? = when (day) {
        today -> "Today"
        today.plusDays(1) -> "Tomorrow"
        today.minusDays(1) -> "Yesterday"
        else -> null
    }

    /** A heading for a day in a list: "Today", "Tomorrow" or "Thursday 15 Oct". */
    fun dayHeading(day: LocalDate, today: LocalDate = LocalDate.now()): String = dayName(day, today) ?: weekday.format(day)

    /** "13 Oct" */
    fun dayMonth(instant: Instant, zone: ZoneId = ZoneId.systemDefault()): String = dayMonth.format(local(instant, zone))

    /** "13/10/2026" */
    fun date(day: LocalDate): String = numeric.format(day)
}

fun platformName(platform: String): String = when (platform) {
    "youtube" -> "YouTube"
    "instagram" -> "Instagram"
    "tiktok" -> "TikTok"
    else -> platform.replaceFirstChar { it.uppercase() }
}
