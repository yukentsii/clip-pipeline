package com.clippipeline.app.ui.common

import android.app.DatePickerDialog
import android.app.TimePickerDialog
import android.content.Context
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.clippipeline.app.data.Account
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime

/** A label over a rounded text box. [onDone] is called with the text when the field is left. */
@Composable
fun LabeledField(
    label: String, value: String, onChange: (String) -> Unit,
    singleLine: Boolean = false, enabled: Boolean = true, onDone: (String) -> Unit = {},
) {
    Column(verticalArrangement = Arrangement.spacedBy(7.dp)) {
        Text(label, style = inter(12, color = Palette.Muted))
        var hadFocus by remember { mutableStateOf(false) }
        BasicTextField(
            value = value, onValueChange = onChange, enabled = enabled, singleLine = singleLine,
            textStyle = inter(14, lineHeight = 20), cursorBrush = SolidColor(Palette.Text),
            modifier = Modifier.fillMaxWidth().onFocusChanged {
                if (hadFocus && !it.isFocused) onDone(value)
                hadFocus = it.isFocused
            },
            decorationBox = { inner ->
                Box(
                    Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(Palette.Card)
                        .border(1.dp, Palette.Line, RoundedCornerShape(14.dp)).padding(horizontal = 14.dp, vertical = 12.dp),
                ) { inner() }
            },
        )
    }
}

/** The tags as chips. Tapping a tag removes it; "＋ add" becomes a small box to type a new one. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun TagsEditor(tags: List<String>, enabled: Boolean = true, onChange: (List<String>) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(7.dp)) {
        Text("Tags", style = inter(12, color = Palette.Muted))
        var adding by remember { mutableStateOf(false) }
        var draft by remember { mutableStateOf("") }
        val commit = {
            val tag = draft.trim().trimStart('#')
            if (tag.isNotEmpty() && tags.none { it.equals(tag, ignoreCase = true) }) onChange(tags + tag)
            draft = ""
            adding = false
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            for (tag in tags) Chip(tag) { if (enabled) onChange(tags - tag) }
            if (adding) {
                val focus = remember { FocusRequester() }
                var hadFocus by remember { mutableStateOf(false) }
                BasicTextField(
                    value = draft, onValueChange = { draft = it.replace(" ", "") }, singleLine = true,
                    textStyle = inter(12), cursorBrush = SolidColor(Palette.Text),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                    keyboardActions = KeyboardActions(onDone = { commit() }),
                    modifier = Modifier.focusRequester(focus).onFocusChanged {
                        if (hadFocus && !it.isFocused) commit()
                        hadFocus = it.isFocused
                    },
                    decorationBox = { inner ->
                        Box(
                            Modifier.widthIn(min = 72.dp).clip(RoundedCornerShape(18.dp)).border(1.dp, Palette.Faint, RoundedCornerShape(18.dp))
                                .padding(horizontal = 13.dp, vertical = 7.dp),
                        ) { inner() }
                    },
                )
                LaunchedEffect(Unit) { focus.requestFocus() }
            } else if (enabled) {
                Chip("＋ add") { adding = true }
            }
        }
    }
}

@Composable
fun Chip(text: String, selected: Boolean = false, onClick: () -> Unit) {
    val shape = RoundedCornerShape(18.dp)
    Box(
        Modifier.clip(shape)
            .then(if (selected) Modifier.background(Palette.Text) else Modifier.border(1.dp, Palette.Line, shape))
            .clickable(onClick = onClick).padding(horizontal = 13.dp, vertical = 7.dp),
    ) { Text(text, style = if (selected) inter(12, FontWeight.SemiBold, color = Palette.Background) else inter(12, color = Palette.Muted)) }
}

/** One choice out of a few, as a pill that fills the width (Framing: Auto / Crop to faces / Full frame). */
@Composable
fun Segmented(options: List<Pair<String, String>>, selected: String, enabled: Boolean = true, onSelect: (String) -> Unit) {
    Row(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(22.dp)).background(Palette.Card).padding(4.dp),
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        for ((value, label) in options) {
            val active = value == selected
            Box(
                Modifier.weight(1f).clip(RoundedCornerShape(18.dp)).background(if (active) Palette.Text else Palette.Card)
                    .clickable(enabled = enabled && !active) { onSelect(value) }.padding(vertical = 9.dp),
                contentAlignment = Alignment.Center,
            ) { Text(label, style = if (active) inter(12, FontWeight.SemiBold, color = Palette.Background) else inter(12, color = Palette.Muted)) }
        }
    }
}

/** Where a clip goes: whether an account is chosen, and a time if the owner picked one (null = its next free slot). */
data class Choice(val chosen: Boolean, val at: Instant? = null)

/** "Post to": one row per account with a tick, its name and platform, and when the clip would go out there. */
@Composable
fun AccountRows(
    accounts: List<Account>, choices: Map<String, Choice>, enabled: Boolean = true,
    slotOf: (Account) -> Instant? = { Times.parse(it.nextSlot) },
    locked: (Account) -> String? = { null },
    onChange: (Map<String, Choice>) -> Unit,
) {
    val context = LocalContext.current
    for (account in accounts) {
        val lockedBecause = locked(account)
        val usable = account.configured && account.enabled && lockedBecause == null
        val choice = choices[account.id] ?: Choice(false)
        val canToggle = enabled && usable
        Row(
            Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(Palette.Card)
                .clickable(enabled = canToggle) { onChange(choices + (account.id to choice.copy(chosen = !choice.chosen))) }
                .padding(horizontal = 14.dp, vertical = 12.dp),
            horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically,
        ) {
            Row(Modifier.weight(1f), horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
                val box = RoundedCornerShape(6.dp)
                if (choice.chosen) {
                    Box(Modifier.size(20.dp).clip(box).background(Palette.Text), contentAlignment = Alignment.Center) {
                        Box(Modifier.size(8.dp).clip(RoundedCornerShape(2.dp)).background(Palette.Background))
                    }
                } else {
                    Box(Modifier.size(20.dp).border(1.dp, Palette.Faint, box))
                }
                Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Text(account.name, style = inter(14, FontWeight.Medium, color = if (choice.chosen || usable) Palette.Text else Palette.Muted), maxLines = 1)
                    Text(platformName(account.platform), style = inter(12, color = Palette.Faint))
                }
            }
            val at = choice.at ?: slotOf(account)
            when {
                lockedBecause != null -> Text(lockedBecause, style = inter(13, color = Palette.Muted))
                !account.configured -> Text("Login needed", style = inter(13, color = Palette.Red))
                !account.enabled -> Text("Switched off", style = inter(13, color = Palette.Muted))
                at == null -> Text("Next free slot", style = inter(13))
                else -> Text(
                    Times.dayAndTime(at), style = inter(13, color = if (choice.chosen) Palette.Text else Palette.Muted),
                    modifier = Modifier.clickable(
                        enabled = enabled, interactionSource = remember { MutableInteractionSource() }, indication = null,
                    ) { pickDateTime(context, at) { picked -> onChange(choices + (account.id to Choice(true, picked))) } },
                )
            }
        }
    }
}

/** The phone's own date picker, then its time picker. */
fun pickDateTime(context: Context, initial: Instant, onPicked: (Instant) -> Unit) {
    val zone = ZoneId.systemDefault()
    val start = initial.atZone(zone)
    DatePickerDialog(context, { _, year, month, day ->
        TimePickerDialog(context, { _, hour, minute ->
            onPicked(ZonedDateTime.of(year, month + 1, day, hour, minute, 0, 0, zone).toInstant())
        }, start.hour, start.minute, true).show()
    }, start.year, start.monthValue - 1, start.dayOfMonth).show()
}
