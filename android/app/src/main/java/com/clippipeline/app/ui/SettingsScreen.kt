package com.clippipeline.app.ui

import android.app.TimePickerDialog
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.clippipeline.app.BuildConfig
import com.clippipeline.app.ClipApp
import com.clippipeline.app.data.Account
import com.clippipeline.app.data.AccountList
import com.clippipeline.app.data.Device
import com.clippipeline.app.data.DeviceList
import com.clippipeline.app.data.Pairing
import com.clippipeline.app.data.Prefs
import com.clippipeline.app.data.SystemInfo
import com.clippipeline.app.ui.common.Chip
import com.clippipeline.app.ui.common.LabeledField
import com.clippipeline.app.ui.common.OutlineButton
import com.clippipeline.app.ui.common.Segmented
import com.clippipeline.app.ui.common.Times
import com.clippipeline.app.ui.common.explain
import com.clippipeline.app.ui.common.platformName
import com.clippipeline.app.ui.common.rememberLoaded
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlin.math.roundToInt

/** Figma frame "8 Settings": accounts, the review score, how new clips are made, paired phones, the server. */
@Composable
fun SettingsScreen(app: ClipApp, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    val accounts = rememberLoaded(app) { app.api.get<AccountList>("/accounts").accounts }
    val prefs = rememberLoaded(app) { app.api.get<Prefs>("/settings") }
    val devices = rememberLoaded(app) { app.api.get<DeviceList>("/devices").devices }
    val system = rememberLoaded(app) { app.api.get<SystemInfo>("/system") }
    var error by remember { mutableStateOf<String?>(null) }
    var open by remember { mutableStateOf<String?>(null) }           // the one row that is unfolded, if any
    val toggle = { key: String -> open = if (open == key) null else key }

    fun run(then: () -> Unit = {}, action: suspend () -> Unit) {
        scope.launch {
            try {
                action()
                error = null
                then()
            } catch (e: Exception) {
                error = app.explain(e)
            }
        }
    }

    Column(Modifier.fillMaxSize().background(Palette.Background).statusBarsPadding().imePadding()) {
        BackHeader("Back", onBack, title = "Settings")
        Column(
            Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState()).navigationBarsPadding()
                .padding(start = 20.dp, end = 20.dp, top = 6.dp, bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            (error ?: accounts.error ?: prefs.error)?.let { Text(it, style = inter(13, lineHeight = 19, color = Palette.Red)) }

            // ------------------------------------------------------------ accounts
            Label("Accounts")
            ListCard {
                val list = accounts.value.orEmpty()
                list.forEachIndexed { index, account ->
                    if (index > 0) Divider()
                    AccountRow(account) { toggle("account-${account.id}") }
                    if (open == "account-${account.id}") {
                        AccountEditor(account) { body -> run(accounts.reload) { app.api.send("PATCH", "/accounts/${account.id}", body) } }
                    }
                }
                if (list.isNotEmpty()) Divider()
                PlainRow("＋  Add an account") { toggle("add-account") }
                if (open == "add-account") {
                    NewAccount { platform, name ->
                        run({ open = null; accounts.reload() }) {
                            app.api.send("POST", "/accounts", buildJsonObject { put("platform", platform); put("name", name) })
                        }
                    }
                }
            }
            Hint("Tap an account to change its posting times or switch it off.")

            // ------------------------------------------------------------ review score
            Label("Review")
            prefs.value?.let { current ->
                var score by remember(current.reviewMinScore) { mutableStateOf(current.reviewMinScore) }
                Column(
                    Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Palette.Card).padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(14.dp),
                ) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        Text("Send me clips scoring at least", style = inter(14, FontWeight.Medium))
                        Text(score.toString(), style = inter(20, FontWeight.Bold))
                    }
                    ScoreSlider(score, onChange = { score = it }) {
                        run(prefs.reload) { app.api.send("PATCH", "/settings", buildJsonObject { put("review_min_score", score) }) }
                    }
                    Text("Lower shows you more clips, higher shows only the strongest.", style = inter(12, lineHeight = 17, color = Palette.Faint))
                }

                // -------------------------------------------------------- render defaults
                Label("New clips are made with")
                ListCard {
                    val choices = listOf(
                        Triple("Shape", "default_aspect", listOf("9:16" to "Vertical 9:16", "1:1" to "Square 1:1", "16:9" to "Wide 16:9")),
                        Triple("Framing", "default_layout", listOf("auto" to "Auto", "crop" to "Crop to faces", "fit" to "Full frame")),
                        Triple("Captions", "default_subtitle_style", listOf("karaoke" to "Word highlight", "static" to "Plain", "none" to "None")),
                    )
                    choices.forEachIndexed { index, (title, key, options) ->
                        val value = when (key) {
                            "default_aspect" -> current.defaultAspect
                            "default_layout" -> current.defaultLayout
                            else -> current.defaultSubtitleStyle
                        }
                        if (index > 0) Divider()
                        ValueRow(title, (options.firstOrNull { it.first == value }?.second ?: value) + "  ›") { toggle(key) }
                        if (open == key) {
                            Box(Modifier.padding(bottom = 13.dp)) {
                                Segmented(options, value) { picked ->
                                    run(prefs.reload) { app.api.send("PATCH", "/settings", buildJsonObject { put(key, picked) }) }
                                }
                            }
                        }
                    }
                }
            }

            // ------------------------------------------------------------ paired phones
            Label("Paired phones")
            ListCard {
                val phones = devices.value.orEmpty().filter { it.revokedAt == null }
                phones.forEachIndexed { index, device ->
                    if (index > 0) Divider()
                    DeviceRow(device) { run(devices.reload) { app.api.send("DELETE", "/devices/${device.id}") } }
                }
                if (phones.isNotEmpty()) Divider()
                PlainRow("＋  Pair another phone") { toggle("pair") }
                if (open == "pair") {
                    Text(
                        "On your server run “python -m clip_pipeline pair”, then open clip-pipeline on the other phone and point its camera at the code.",
                        style = inter(13, lineHeight = 19, color = Palette.Muted), modifier = Modifier.padding(bottom = 13.dp),
                    )
                }
            }

            // ------------------------------------------------------------ server
            Label("Server")
            ListCard {
                val server = app.session.current?.url
                ValueRow("Address", (server?.let { "${it.host}:${it.port}" } ?: "") + "  ›") { toggle("server") }
                if (open == "server") {
                    Column(Modifier.padding(bottom = 13.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                        Text(
                            "${server ?: ""}\nUnpairing removes this phone from the paired phones and makes it forget the server. To use it again you pair again with a new code.",
                            style = inter(13, lineHeight = 19, color = Palette.Muted),
                        )
                        var sure by remember { mutableStateOf(false) }
                        OutlineButton(if (sure) "Yes, unpair this phone" else "Unpair this phone") {
                            if (!sure) sure = true else scope.launch {
                                // Take this phone off the server's list too, if the server can be reached.
                                app.session.current?.deviceId?.let { id -> runCatching { app.api.send("DELETE", "/devices/$id") } }
                                Pairing(app).forget()
                            }
                        }
                    }
                }
                Divider()
                ValueRow("Video encoder", system.value?.encoder?.let { if (it == "libx264") "Software" else "Hardware" } ?: "")
                Divider()
                ValueRow("Version", BuildConfig.VERSION_NAME)
            }
        }
    }
}

@Composable
private fun Label(text: String) = Text(text, style = inter(12, FontWeight.SemiBold, color = Palette.Muted))

@Composable
private fun Hint(text: String) = Text(text, style = inter(12, lineHeight = 17, color = Palette.Faint))

@Composable
private fun ListCard(content: @Composable ColumnScope.() -> Unit) {
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Palette.Card).padding(horizontal = 14.dp, vertical = 4.dp),
        content = content,
    )
}

@Composable
private fun Divider() = Box(Modifier.fillMaxWidth().height(1.dp).background(Palette.Line))

@Composable
private fun PlainRow(text: String, onClick: () -> Unit) {
    Text(text, style = inter(14, FontWeight.Medium), modifier = Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 13.dp))
}

@Composable
private fun ValueRow(title: String, value: String, onClick: (() -> Unit)? = null) {
    Row(
        Modifier.fillMaxWidth().then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier).padding(vertical = 13.dp),
        horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(title, style = inter(14, FontWeight.Medium))
        Text(value, style = inter(13, color = Palette.Muted))
    }
}

@Composable
private fun TwoLineRow(title: String, detail: String, onClick: (() -> Unit)? = null, trailing: @Composable () -> Unit) {
    Row(
        Modifier.fillMaxWidth().then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier).padding(vertical = 13.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
            Text(title, style = inter(14, FontWeight.Medium), maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(detail, style = inter(12, lineHeight = 17, color = Palette.Faint))
        }
        trailing()
    }
}

@Composable
private fun AccountRow(account: Account, onClick: () -> Unit) {
    TwoLineRow(account.name, "${platformName(account.platform)} · ${account.slots.joinToString("  ").ifEmpty { "no posting times" }}", onClick) {
        val (text, color) = when {
            !account.enabled -> "Switched off" to Palette.Muted
            account.configured -> "Connected" to Palette.Green
            else -> "Login needed" to Palette.Red
        }
        Text(text, style = inter(12, FontWeight.Medium, color = color))
    }
}

/** Unfolds under an account: its daily posting times as chips, and switching it off or on. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun AccountEditor(account: Account, patch: (JsonObject) -> Unit) {
    val context = LocalContext.current
    fun slots(times: List<String>) = patch(buildJsonObject { putJsonArray("slots") { times.sorted().forEach(::add) } })
    Column(Modifier.padding(bottom = 13.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Hint("Posting times each day. Tap one to remove it.")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            for (slot in account.slots) Chip(slot) { if (account.slots.size > 1) slots(account.slots - slot) }
            Chip("＋ add") {
                TimePickerDialog(context, { _, hour, minute ->
                    val time = "%02d:%02d".format(hour, minute)
                    if (time !in account.slots) slots(account.slots + time)
                }, 12, 0, true).show()
            }
        }
        if (!account.configured) Hint("Its login is added on the server, from a computer.")
        OutlineButton(if (account.enabled) "Switch off" else "Switch on") { patch(buildJsonObject { put("enabled", !account.enabled) }) }
    }
}

@Composable
private fun NewAccount(onAdd: (platform: String, name: String) -> Unit) {
    var platform by remember { mutableStateOf("youtube") }
    var name by remember { mutableStateOf("") }
    Column(Modifier.padding(bottom = 13.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Segmented(listOf("youtube" to "YouTube", "instagram" to "Instagram", "tiktok" to "TikTok"), platform) { platform = it }
        LabeledField("Name", name, { name = it.take(60) }, singleLine = true)
        Hint("The account can post once its login has been added on the server, from a computer.")
        OutlineButton("Add", enabled = name.isNotBlank()) { onAdd(platform, name.trim()) }
    }
}

@Composable
private fun DeviceRow(device: Device, onRevoke: () -> Unit) {
    val paired = Times.parse(device.createdAt)?.let(Times::dayMonth)
    val seen = Times.parse(device.lastSeenAt)?.let(Times::dayMonth)
    val detail = when {
        device.thisDevice -> "This phone · paired ${paired.orEmpty()}"
        seen != null -> "Last seen $seen"
        else -> "Paired ${paired.orEmpty()} · not used yet"
    }
    TwoLineRow(device.name, detail) {
        if (device.thisDevice) {
            Text("Active", style = inter(12, FontWeight.Medium, color = Palette.Green))
        } else {
            var sure by remember { mutableStateOf(false) }
            OutlineButton(if (sure) "Sure?" else "Revoke") { if (!sure) sure = true else onRevoke() }
        }
    }
}

/** The score slider of the file: a 4dp track, a 20dp round thumb, 0 to 100. */
@Composable
private fun ScoreSlider(value: Int, onChange: (Int) -> Unit, onRelease: () -> Unit) {
    val change by rememberUpdatedState(onChange)
    val release by rememberUpdatedState(onRelease)
    BoxWithConstraints(Modifier.fillMaxWidth().height(20.dp)) {
        val thumb = 20.dp
        val travel = maxWidth - thumb
        val widthPx = constraints.maxWidth.toFloat()
        val thumbPx = widthPx * (thumb / maxWidth)
        fun at(x: Float) = (((x - thumbPx / 2) / (widthPx - thumbPx)).coerceIn(0f, 1f) * 100).roundToInt()
        Box(
            Modifier.fillMaxSize()
                .pointerInput(Unit) { detectTapGestures { change(at(it.x)); release() } }
                .pointerInput(Unit) {
                    detectHorizontalDragGestures(onDragEnd = { release() }) { pointer, _ -> change(at(pointer.position.x)) }
                },
            contentAlignment = Alignment.CenterStart,
        ) {
            Box(Modifier.fillMaxWidth().height(4.dp).clip(RoundedCornerShape(2.dp)).background(Palette.Track))
            Box(Modifier.fillMaxWidth(value / 100f).height(4.dp).clip(RoundedCornerShape(2.dp)).background(Palette.Text))
            Box(Modifier.offset(x = travel * (value / 100f)).size(thumb).clip(CircleShape).background(Color(0xFFF3EFE6)))
        }
    }
}
