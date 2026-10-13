package com.clippipeline.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.clippipeline.app.ClipApp
import com.clippipeline.app.data.Account
import com.clippipeline.app.data.AccountList
import com.clippipeline.app.data.Post
import com.clippipeline.app.ui.common.AccountRows
import com.clippipeline.app.ui.common.Choice
import com.clippipeline.app.ui.common.LabeledField
import com.clippipeline.app.ui.common.LoadedContent
import com.clippipeline.app.ui.common.OutlineButton
import com.clippipeline.app.ui.common.TagsEditor
import com.clippipeline.app.ui.common.Times
import com.clippipeline.app.ui.common.explain
import com.clippipeline.app.ui.common.rememberLoaded
import com.clippipeline.app.ui.theme.Palette
import com.clippipeline.app.ui.theme.inter
import kotlinx.coroutines.launch
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray

/**
 * Opened from a card on the Schedule screen ("Tap to edit text, time or accounts"). The design has
 * no frame for it, so it reuses the parts of the Clip detail screen: the same fields, the same
 * "Post to" rows and the same two buttons at the bottom.
 */
@Composable
fun PostDetailScreen(app: ClipApp, postId: String, onBack: () -> Unit) {
    val post = rememberLoaded(app, postId) { app.api.get<Post>("/posts/$postId") }
    val accounts = rememberLoaded(app) { app.api.get<AccountList>("/accounts").accounts }
    Column(Modifier.fillMaxSize().background(Palette.Background).statusBarsPadding().imePadding()) {
        BackHeader("Schedule", onBack)
        val loadedPost = post.value
        val loadedAccounts = accounts.value
        if (loadedPost == null || loadedAccounts == null) {
            Box(Modifier.padding(20.dp)) { LoadedContent(if (loadedPost == null) post else accounts) {} }
        } else {
            PostEditor(app, loadedPost, loadedAccounts, onBack)
        }
    }
}

@Composable
private fun ColumnScope.PostEditor(app: ClipApp, first: Post, accounts: List<Account>, onDone: () -> Unit) {
    val scope = rememberCoroutineScope()
    var post by remember { mutableStateOf(first) }
    var title by remember { mutableStateOf(first.title) }
    var description by remember { mutableStateOf(first.description) }
    var tags by remember { mutableStateOf(first.tags) }
    val byAccount = post.targets.associateBy { it.accountId }
    var choices by remember {
        mutableStateOf(first.targets.mapNotNull { t -> t.accountId?.let { it to Choice(true, Times.parse(t.scheduledAt)) } }.toMap())
    }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var confirmCancel by remember { mutableStateOf(false) }

    fun run(action: suspend () -> Unit) {
        busy = true
        error = null
        scope.launch {
            try {
                action()
            } catch (e: Exception) {
                error = app.explain(e)
            }
            busy = false
        }
    }

    Column(
        Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState())
            .padding(start = 20.dp, end = 20.dp, top = 8.dp, bottom = 20.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp),
    ) {
        Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
            ClipPlayer(app, post.clipId?.let { "/clips/$it/video" }, poster = post.clipId?.let { "/clips/$it/thumb" })
        }
        LabeledField("Title", title, { title = it.take(100) }, enabled = post.editable)
        LabeledField("Description", description, { description = it.take(2200) }, enabled = post.editable)
        TagsEditor(tags, enabled = post.editable) { tags = it }

        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Post to", style = inter(12, color = Palette.Muted))
            // An account that already posted this clip stays as it is; nothing is ever removed from a platform.
            AccountRows(
                accounts, choices, enabled = !busy && post.editable,
                slotOf = { account -> Times.parse(byAccount[account.id]?.scheduledAt ?: account.nextSlot) },
                locked = { account ->
                    byAccount[account.id]?.takeIf { it.status == "completed" }
                        ?.let { "Posted " + (Times.parse(it.postedAt ?: it.scheduledAt)?.let(Times::dayAndTime) ?: "") }
                },
            ) { choices = it }
            Text("Tap a time to change it. An account you add gets its next free slot.", style = inter(12, lineHeight = 17, color = Palette.Faint))
        }

        val failed = post.targets.filter { it.status == "failed" }
        if (failed.isNotEmpty()) {
            Column(
                Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Palette.Card)
                    .border(1.dp, Palette.Red.copy(alpha = 0.35f), RoundedCornerShape(18.dp)).padding(14.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                for (target in failed) {
                    TargetRow(target.copy(error = null))
                    Text(target.error.orEmpty().trim().ifBlank { "The upload failed." }, style = inter(13, lineHeight = 19, color = Palette.Muted))
                }
                OutlineButton("Try again", enabled = !busy) {
                    run {
                        app.api.send("POST", "/posts/${post.id}/retry")
                        post = app.api.get("/posts/${post.id}")
                    }
                }
            }
        }
    }

    Column(Modifier.fillMaxWidth().background(Palette.Background)) {
        Box(Modifier.fillMaxWidth().height(1.dp).background(Palette.Line))
        val notice = error ?: if (confirmCancel) "Cancel what has not been posted yet? If nothing went out, the clip goes back to Review." else null
        notice?.let {
            Text(
                it, style = inter(13, lineHeight = 19, color = if (error != null) Palette.Red else Palette.Muted),
                modifier = Modifier.padding(start = 20.dp, end = 20.dp, top = 12.dp),
            )
        }
        val wanted = accounts.filter { choices[it.id]?.chosen == true && byAccount[it.id]?.status != "completed" }
        val canSave = !busy && post.editable && title.isNotBlank() && wanted.isNotEmpty()
        Row(
            Modifier.fillMaxWidth().navigationBarsPadding().padding(start = 20.dp, end = 20.dp, top = 14.dp, bottom = 14.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            val shape = RoundedCornerShape(26.dp)
            Box(
                Modifier.weight(1f).alpha(if (busy || !post.editable) 0.5f else 1f).clip(shape).border(1.dp, Palette.Line, shape)
                    .clickable(enabled = !busy && post.editable) {
                        if (!confirmCancel) {
                            confirmCancel = true
                        } else {
                            run {
                                app.api.send("DELETE", "/posts/${post.id}")
                                onDone()
                            }
                        }
                    }.padding(vertical = 15.dp),
                contentAlignment = Alignment.Center,
            ) { Text(if (confirmCancel) "Yes, cancel it" else "Cancel post", style = inter(15, FontWeight.Medium)) }
            Box(
                Modifier.weight(1f).alpha(if (canSave) 1f else 0.5f).clip(shape).background(Palette.Text).clickable(enabled = canSave) {
                    run {
                        app.api.send("PATCH", "/posts/${post.id}", buildJsonObject {
                            put("title", title.trim())
                            put("description", description)
                            putJsonArray("tags") { tags.forEach(::add) }
                            putJsonArray("targets") {
                                for (account in wanted) add(buildJsonObject {
                                    put("account_id", account.id)
                                    val picked = choices[account.id]?.at
                                    // A time is sent only where it was changed (or chosen for a newly added account).
                                    if (picked != null && picked != Times.parse(byAccount[account.id]?.scheduledAt)) put("scheduled_at", picked.toString())
                                })
                            }
                        })
                        onDone()
                    }
                }.padding(vertical = 15.dp),
                contentAlignment = Alignment.Center,
            ) { Text("Save", style = inter(15, FontWeight.SemiBold, color = Palette.Background)) }
        }
    }
}
