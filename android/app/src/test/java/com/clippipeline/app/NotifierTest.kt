package com.clippipeline.app

import com.clippipeline.app.data.Event
import com.clippipeline.app.data.EventList
import com.clippipeline.app.data.Notifier
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Test

class NotifierTest {
    private fun event(id: Long) = Event(id = id, kind = "upload_done", title = "Posted $id")

    @Test
    fun theFirstCheckRemembersWhereItIsAndAnnouncesNothing() {
        val (announce, newest) = Notifier.plan(lastSeen = null, EventList(listOf(event(7)), lastId = 7))
        assertEquals(emptyList<Event>(), announce)
        assertEquals(7L, newest)
    }

    @Test
    fun laterChecksAnnounceOnlyWhatIsNewOldestFirst() {
        val (announce, newest) = Notifier.plan(lastSeen = 7, EventList(listOf(event(9), event(8), event(7)), lastId = 9))
        assertEquals(listOf(8L, 9L), announce.map { it.id })
        assertEquals(9L, newest)
    }

    @Test
    fun nothingNewKeepsTheSamePlace() {
        val (announce, newest) = Notifier.plan(lastSeen = 9, EventList(emptyList(), lastId = 9))
        assertEquals(emptyList<Event>(), announce)
        assertEquals(9L, newest)
    }

    @Test
    fun anEmptyServerOnTheFirstCheckStartsAtZero() {
        val (announce, newest) = Notifier.plan(lastSeen = null, EventList(emptyList(), lastId = 0))
        assertEquals(emptyList<Event>(), announce)
        assertEquals(0L, newest)
    }

    @Test
    fun readsTheEventsTheBackendSends() {
        // A real answer of GET /events from the backend.
        val body = """{"events": [{"id": 1, "kind": "clips_ready", "title": "5 clips ready", "body": "Eat Everything In A Grocery Store",
            "data": {"source_id": "6e09b2dea5b04014a76b69b460b9ff76"}, "created_at": "2026-10-12T23:03:30Z"}], "last_id": 1}"""
        val list = Json { ignoreUnknownKeys = true }.decodeFromString<EventList>(body)
        assertEquals("5 clips ready", list.events.single().title)
        assertEquals(1L, list.lastId)
    }
}
