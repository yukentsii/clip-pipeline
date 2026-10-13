package com.clippipeline.app.ui.common

import android.graphics.BitmapFactory
import android.util.LruCache
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.clippipeline.app.ClipApp
import com.clippipeline.app.data.ApiException
import com.clippipeline.app.data.Pairing
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import okhttp3.Request
import java.io.IOException

/**
 * Turns a failed call into words. If the server says this phone is no longer paired (revoked, or
 * the server was reset), the phone forgets the server and the app returns to the pairing screen.
 */
suspend fun ClipApp.explain(e: Throwable): String {
    if (e is ApiException && e.status == 401 && (e.message.orEmpty().contains("revoked") || e.message == "unknown device")) {
        Pairing(this).forget()
    }
    return when (e) {
        is ApiException -> e.message ?: "The server answered ${e.status}"
        is IOException -> "Cannot reach the server. Check that this phone is on the same network."
        is SecurityException -> "clip-pipeline was not allowed to read that file."
        else -> e.message ?: "Something went wrong"
    }
}

/** What a screen knows about the data it shows. `value` stays while a refresh is under way. */
class Loaded<T>(val value: T?, val error: String?, val reload: () -> Unit)

/** Loads when first shown, on [Loaded.reload], and every [refreshMillis] while on screen (0 = never). */
@Composable
fun <T> rememberLoaded(app: ClipApp, vararg keys: Any?, refreshMillis: Long = 0, load: suspend () -> T): Loaded<T> {
    var value by remember(*keys) { mutableStateOf<T?>(null) }
    var error by remember(*keys) { mutableStateOf<String?>(null) }
    var tick by remember { mutableIntStateOf(0) }
    LaunchedEffect(tick, *keys) {
        while (true) {
            try {
                value = load()
                error = null
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                error = app.explain(e)
            }
            if (refreshMillis <= 0) break
            delay(refreshMillis)
        }
    }
    return Loaded(value, error) { tick++ }
}

/** The states the design leaves open: still loading, failed, and nothing to show. */
@Composable
fun <T> LoadedContent(loaded: Loaded<T>, content: @Composable (T) -> Unit) {
    val value = loaded.value
    when {
        value != null -> content(value)
        loaded.error != null -> Column(verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Text(loaded.error, style = inter(13, lineHeight = 19, color = Palette.Red))
            OutlineButton("Try again", onClick = loaded.reload)
        }
        else -> Text("Loading…", style = inter(13, color = Palette.Muted))
    }
}

@Composable
fun OutlineButton(text: String, modifier: Modifier = Modifier, enabled: Boolean = true, onClick: () -> Unit) {
    Box(
        modifier.alpha(if (enabled) 1f else 0.5f).clip(RoundedCornerShape(18.dp)).border(1.dp, Palette.Line, RoundedCornerShape(18.dp))
            .clickable(enabled = enabled, onClick = onClick).padding(horizontal = 16.dp, vertical = 9.dp),
        contentAlignment = Alignment.Center,
    ) { Text(text, style = inter(12, FontWeight.Medium)) }
}

private val images = LruCache<String, ImageBitmap>(60)

/** A picture from the server (a thumbnail), fetched with the same signed client as everything else. */
@Composable
fun RemoteImage(app: ClipApp, path: String?, modifier: Modifier = Modifier, placeholder: Brush = thumbnailPlaceholder) {
    var bitmap by remember(path) { mutableStateOf(path?.let(images::get)) }
    LaunchedEffect(path) {
        if (bitmap == null && path != null) {
            bitmap = withContext(Dispatchers.IO) {
                runCatching {
                    app.http.newCall(Request.Builder().url(app.api.url(path)).build()).execute().use { response ->
                        if (response.isSuccessful) BitmapFactory.decodeStream(response.body.byteStream())?.asImageBitmap() else null
                    }
                }.getOrNull()
            }?.also { images.put(path, it) }
        }
    }
    Box(modifier.background(placeholder)) {
        bitmap?.let { Image(it, contentDescription = null, modifier = Modifier.matchParentSize(), contentScale = ContentScale.Crop) }
    }
}

val thumbnailPlaceholder = Brush.linearGradient(
    0f to Color(0xFF2A2F36), 0.714f to Color(0xFF15181C), start = Offset(0f, 0f), end = Offset(200f, 260f),
)

/** Green for the strongest clips, the plain text colour for the rest, as in the file. */
fun scoreColor(score: Int): Color = if (score >= 80) Palette.Green else Palette.Text
