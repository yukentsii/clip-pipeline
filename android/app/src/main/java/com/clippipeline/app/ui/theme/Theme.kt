package com.clippipeline.app.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontVariation
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.sp
import com.clippipeline.app.R

/** The colours of the Figma file. */
object Palette {
    val Background = Color(0xFF0C0D0E)
    val Card = Color(0xFF15171A)
    val Track = Color(0xFF1E2125)
    val Line = Color(0xFF272A2F)
    val Text = Color(0xFFF3EFE6)        // also the only accent
    val Muted = Color(0xFF8C8980)
    val Faint = Color(0xFF5C5A55)
    val Green = Color(0xFF7ED9A0)
    val Red = Color(0xFFE88080)
    val Amber = Color(0xFFE9C46A)
}

private fun interFont(weight: FontWeight) =
    Font(R.font.inter, weight, variationSettings = FontVariation.Settings(FontVariation.weight(weight.weight)))

val Inter = FontFamily(
    interFont(FontWeight.Normal), interFont(FontWeight.Medium), interFont(FontWeight.SemiBold), interFont(FontWeight.Bold),
)

/** A text style from the file: size and line height in the same units Figma shows. */
fun inter(size: Int, weight: FontWeight = FontWeight.Normal, lineHeight: Int? = null, color: Color = Palette.Text) =
    TextStyle(
        fontFamily = Inter, fontSize = size.sp, fontWeight = weight, color = color,
        lineHeight = lineHeight?.sp ?: TextUnit.Unspecified,
    )

@Composable
fun ClipPipelineTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(
            primary = Palette.Text, onPrimary = Palette.Background,
            background = Palette.Background, onBackground = Palette.Text,
            surface = Palette.Card, onSurface = Palette.Text,
            surfaceVariant = Palette.Track, onSurfaceVariant = Palette.Muted,
            outline = Palette.Line, error = Palette.Red,
        ),
        content = content,
    )
}
