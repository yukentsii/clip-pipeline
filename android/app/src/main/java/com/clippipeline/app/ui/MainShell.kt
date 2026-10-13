package com.clippipeline.app.ui

import android.Manifest
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.clippipeline.app.ClipApp
import com.clippipeline.app.R
import com.clippipeline.app.data.Notifier
import com.clippipeline.app.data.Status
import kotlinx.coroutines.delay
import com.clippipeline.app.ui.common.rememberLoaded
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter

enum class Tab(val label: String) { Review("Review"), Add("Add"), Schedule("Schedule"), Insights("Insights") }

/** A screen that covers the tabs. */
sealed interface Overlay {
    data class ClipDetail(val clipId: String, val position: Int, val total: Int) : Overlay
    data class PostDetail(val postId: String) : Overlay
    data object Settings : Overlay
    data object StatusScreen : Overlay
}

@Composable
fun MainShell(app: ClipApp) {
    var tab by rememberSaveable { mutableStateOf(Tab.Review) }
    var overlay by remember { mutableStateOf<Overlay?>(null) }
    // The bell's dot: something needs attention. Checked while the app is open.
    val status = rememberLoaded(app, refreshMillis = 20_000) { app.api.get<Status>("/status") }
    val attention = status.value?.needsAttention?.isNotEmpty() == true
    val close = { overlay = null }

    // Notifications: ask to be allowed once, and while the app is open check for news every half minute
    // (in the background a scheduled job does it, see Notifier).
    val askNotifications = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    LaunchedEffect(Unit) {
        if (Build.VERSION.SDK_INT >= 33) askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        while (true) {
            runCatching { Notifier.check(app) }
            delay(30_000)
        }
    }

    // Something shared from another app: show the Add tab, where it appears.
    val arrivals by app.intake.arrivals.collectAsState()
    LaunchedEffect(arrivals) {
        if (arrivals > 0) {
            overlay = null
            tab = Tab.Add
        }
    }

    BackHandler(enabled = overlay != null || tab != Tab.Review) {
        if (overlay != null) overlay = null else tab = Tab.Review
    }

    Box(Modifier.fillMaxSize().background(Palette.Background)) {
        when (val current = overlay) {
            is Overlay.ClipDetail -> ClipDetailScreen(app, current, onBack = close, onDecided = { close(); status.reload() })
            is Overlay.PostDetail -> PostDetailScreen(app, current.postId, onBack = close)
            Overlay.Settings -> SettingsScreen(app, onBack = close)
            Overlay.StatusScreen -> StatusScreen(app, onBack = { close(); status.reload() }, onSettings = { overlay = Overlay.Settings })
            null -> Column(Modifier.fillMaxSize().statusBarsPadding()) {
                TopBar(tab.label, attention, onSettings = { overlay = Overlay.Settings }, onStatus = { overlay = Overlay.StatusScreen })
                Box(Modifier.weight(1f).fillMaxWidth()) {
                    when (tab) {
                        Tab.Review -> ReviewScreen(app) { clip, position, total -> overlay = Overlay.ClipDetail(clip.id, position, total) }
                        Tab.Add -> AddScreen(app)
                        Tab.Schedule -> ScheduleScreen(app) { overlay = Overlay.PostDetail(it.id) }
                        Tab.Insights -> InsightsScreen(app)
                    }
                }
                BottomNav(tab) { tab = it }
            }
        }
    }
}

@Composable
private fun TopBar(title: String, attention: Boolean, onSettings: () -> Unit, onStatus: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(start = 20.dp, end = 20.dp, top = 14.dp, bottom = 10.dp),
        horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(title, style = inter(28, FontWeight.SemiBold, lineHeight = 34))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            RoundIconButton(R.drawable.ic_settings, "Settings", onSettings)
            Box {
                RoundIconButton(R.drawable.ic_notifications, "Status", onStatus)
                if (attention) {
                    Box(
                        Modifier.offset(x = 26.dp, y = 6.dp).size(9.dp).clip(CircleShape).background(Palette.Background)
                            .padding(2.dp).clip(CircleShape).background(Palette.Amber)
                    )
                }
            }
        }
    }
}

@Composable
private fun RoundIconButton(icon: Int, description: String, onClick: () -> Unit) {
    Box(
        Modifier.size(40.dp).clip(CircleShape).border(1.dp, Palette.Line, CircleShape).clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) { Icon(painterResource(icon), contentDescription = description, tint = Palette.Text, modifier = Modifier.size(20.dp)) }
}

@Composable
private fun BottomNav(selected: Tab, onSelect: (Tab) -> Unit) {
    Column(Modifier.fillMaxWidth().background(Palette.Background)) {
        Box(Modifier.fillMaxWidth().height(1.dp).background(Palette.Line))
        Row(Modifier.fillMaxWidth().navigationBarsPadding().padding(start = 12.dp, end = 12.dp, top = 10.dp, bottom = 10.dp)) {
            for (tab in Tab.entries) {
                val active = tab == selected
                Column(
                    Modifier.weight(1f)
                        .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null) { onSelect(tab) }
                        .padding(vertical = 6.dp),
                    horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    Box(
                        Modifier.width(if (active) 28.dp else 20.dp).height(4.dp).clip(RoundedCornerShape(2.dp))
                            .background(if (active) Palette.Text else Palette.Faint)
                    )
                    Text(
                        tab.label,
                        style = if (active) inter(12, FontWeight.SemiBold) else inter(12, color = Palette.Muted),
                    )
                }
            }
        }
    }
}

/** The header of the screens that open over the tabs: "‹  Back" and, on some, a large title. */
@Composable
fun BackHeader(label: String, onBack: () -> Unit, trailing: String? = null, title: String? = null) {
    Column(
        Modifier.fillMaxWidth().padding(start = 20.dp, end = 20.dp, top = 12.dp, bottom = if (title != null) 10.dp else 8.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
            Text(
                "‹  $label", style = inter(15, FontWeight.Medium, color = Palette.Muted),
                modifier = Modifier.clickable(interactionSource = remember { MutableInteractionSource() }, indication = null, onClick = onBack),
            )
            if (trailing != null) Text(trailing, style = inter(13, color = Palette.Faint))
        }
        if (title != null) Text(title, style = inter(28, FontWeight.SemiBold, lineHeight = 34))
    }
}
