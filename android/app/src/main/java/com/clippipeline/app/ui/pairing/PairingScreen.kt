package com.clippipeline.app.ui.pairing

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.clippipeline.app.ClipApp
import com.clippipeline.app.data.Pairing
import com.clippipeline.app.data.PairingLink
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter
import kotlinx.coroutines.launch

/** Figma frames 7 (scan), 7b (type), 7c (camera not allowed), 7d (in progress), 7e (failed). */
@Composable
fun PairingScreen(app: ClipApp) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val pairing = remember { Pairing(app) }

    var typing by rememberSaveable { mutableStateOf(false) }
    var address by rememberSaveable { mutableStateOf("") }
    var code by rememberSaveable { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var refusedScan by remember { mutableStateOf<String?>(null) }      // a QR code that already failed: not tried again

    var cameraAllowed by remember {
        mutableStateOf(ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED)
    }
    var askedForCamera by rememberSaveable { mutableStateOf(false) }
    val askCamera = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        cameraAllowed = granted
        askedForCamera = true
    }

    fun pair(url: String, oneTimeCode: String, scanned: String? = null) {
        if (busy) return
        busy = true
        error = null
        scope.launch {
            error = pairing.pair(url, oneTimeCode)
            if (error != null) refusedScan = scanned
            busy = false
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Palette.Background)
            .systemBarsPadding()
            .imePadding()
            .verticalScroll(rememberScrollState())
            .padding(start = 28.dp, end = 28.dp, top = 56.dp, bottom = 40.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(22.dp),
    ) {
        Text("clip-pipeline", style = inter(15, FontWeight.SemiBold, color = Palette.Muted))
        Text("Pair this phone", style = inter(30, FontWeight.SemiBold, lineHeight = 36))
        Text(
            if (typing) "On your server run “python -m clip_pipeline pair”, then type the server address and the code it shows."
            else "On your server run “python -m clip_pipeline pair”, then point the camera at the code it shows.",
            style = inter(14, lineHeight = 21, color = Palette.Muted), textAlign = TextAlign.Center,
            modifier = Modifier.width(340.dp),
        )

        if (typing) {
            Column(
                Modifier.fillMaxWidth().alpha(if (busy) 0.5f else 1f),
                verticalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                Field("Server", address, "Address shown after “server:”", !busy, KeyboardType.Uri) { address = it }
                Field("Code", code, "Code shown after “code:”", !busy, KeyboardType.Ascii) { code = it }
            }
        } else {
            Box(
                Modifier.size(264.dp).clip(RoundedCornerShape(24.dp)).background(Palette.Card),
                contentAlignment = Alignment.Center,
            ) {
                when {
                    !cameraAllowed -> Column(
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(16.dp),
                    ) {
                        Text(
                            "The camera is off for clip-pipeline.", style = inter(14, lineHeight = 21, color = Palette.Muted),
                            textAlign = TextAlign.Center, modifier = Modifier.width(200.dp),
                        )
                        Box(
                            Modifier.clip(RoundedCornerShape(22.dp)).background(Palette.Text).clickable {
                                if (askedForCamera) {
                                    // Android stops showing its own question after a refusal; the app's settings page still can.
                                    context.startActivity(
                                        Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", context.packageName, null))
                                    )
                                    askedForCamera = false
                                } else {
                                    askCamera.launch(Manifest.permission.CAMERA)
                                }
                            }.padding(horizontal = 22.dp, vertical = 12.dp),
                        ) { Text("Allow camera", style = inter(14, FontWeight.SemiBold, color = Palette.Background)) }
                    }

                    busy -> Text("Pairing…", style = inter(14, FontWeight.SemiBold))

                    else -> {
                        QrScanner(Modifier.fillMaxSize()) { text ->
                            if (busy || text == refusedScan) return@QrScanner
                            val link = PairingLink.parse(text)
                            if (link == null) {
                                refusedScan = text
                                error = "That is not a clip-pipeline pairing code."
                            } else {
                                pair(link.url, link.code, scanned = text)
                            }
                        }
                        Corners()
                    }
                }
            }
        }

        error?.let {
            Text(it, style = inter(13, lineHeight = 19, color = Palette.Red), textAlign = TextAlign.Center, modifier = Modifier.width(320.dp))
        }

        if (typing) {
            Box(
                Modifier.fillMaxWidth().alpha(if (busy) 0.5f else 1f).clip(RoundedCornerShape(26.dp)).background(Palette.Text)
                    .clickable(enabled = !busy) { pair(address, code) }.padding(vertical = 16.dp),
                contentAlignment = Alignment.Center,
            ) { Text(if (busy) "Pairing…" else "Pair", style = inter(14, FontWeight.SemiBold, color = Palette.Background)) }
        }

        Text(
            "The key that proves it is you is made on this phone and never leaves it.",
            style = inter(12, lineHeight = 18, color = Palette.Faint), textAlign = TextAlign.Center, modifier = Modifier.width(300.dp),
        )

        Box(
            Modifier.clip(RoundedCornerShape(26.dp)).border(1.dp, Palette.Line, RoundedCornerShape(26.dp))
                .clickable(enabled = !busy) {
                    typing = !typing
                    error = null
                    if (!typing && !cameraAllowed && !askedForCamera) askCamera.launch(Manifest.permission.CAMERA)
                }
                .padding(horizontal = 26.dp, vertical = 14.dp),
        ) { Text(if (typing) "Scan the code instead" else "Type the code instead", style = inter(14, FontWeight.Medium)) }
    }

    // Ask for the camera once, the first time the scanner is shown.
    androidx.compose.runtime.LaunchedEffect(Unit) {
        if (!cameraAllowed && !askedForCamera) askCamera.launch(Manifest.permission.CAMERA)
    }
}

@Composable
private fun Field(label: String, value: String, placeholder: String, enabled: Boolean, keyboard: KeyboardType, onChange: (String) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
        Text(label, style = inter(12, FontWeight.SemiBold, color = Palette.Muted))
        BasicTextField(
            value = value, onValueChange = onChange, enabled = enabled, singleLine = true,
            textStyle = inter(15), cursorBrush = SolidColor(Palette.Text),
            keyboardOptions = KeyboardOptions(keyboardType = keyboard, autoCorrectEnabled = false),
            modifier = Modifier.fillMaxWidth(),
            decorationBox = { inner ->
                Box(
                    Modifier.fillMaxWidth().clip(RoundedCornerShape(28.dp)).background(Palette.Card)
                        .border(1.dp, Palette.Line, RoundedCornerShape(28.dp)).padding(18.dp),
                ) {
                    if (value.isEmpty()) Text(placeholder, style = inter(15, color = Palette.Faint))
                    inner()
                }
            },
        )
    }
}

/** The four corner marks over the camera picture. */
@Composable
private fun BoxScope.Corners() {
    val inset = 18.dp
    for ((alignment, horizontal, vertical) in listOf(
        Triple(Alignment.TopStart, 1, 1), Triple(Alignment.TopEnd, -1, 1),
        Triple(Alignment.BottomStart, 1, -1), Triple(Alignment.BottomEnd, -1, -1),
    )) {
        Box(Modifier.align(alignment).padding(inset).size(34.dp)) {
            val h = if (vertical > 0) Alignment.TopStart else Alignment.BottomStart
            val v = if (horizontal > 0) Alignment.TopStart else Alignment.TopEnd
            Box(Modifier.align(h).size(34.dp, 4.dp).clip(RoundedCornerShape(2.dp)).background(Palette.Text))
            Box(Modifier.align(v).size(4.dp, 34.dp).clip(RoundedCornerShape(2.dp)).background(Palette.Text))
        }
    }
}
