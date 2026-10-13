package com.clippipeline.app

import com.clippipeline.app.data.PairingLink
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PairingLinkTest {
    @Test
    fun readsTheLinkTheServerPrints() {
        // The exact form asserted in the backend's tests/test_auth.py (test_cli_pair_and_devices).
        val link = PairingLink.parse("clip-pipeline://pair?url=http%3A%2F%2F192.168.1.20%3A8123&code=Qm3xT9cV1kLw8ZrP5nHs2aYd-_xy")
        assertEquals(PairingLink("http://192.168.1.20:8123", "Qm3xT9cV1kLw8ZrP5nHs2aYd-_xy"), link)
    }

    @Test
    fun anHttpsServerIsReadTheSameWay() {
        assertEquals(
            PairingLink("https://clips.example.test", "abcdefgh12345678"),
            PairingLink.parse("clip-pipeline://pair?url=https%3A%2F%2Fclips.example.test&code=abcdefgh12345678"),
        )
    }

    @Test
    fun anythingElseIsNotAPairingCode() {
        for (text in listOf(
            "", "hello", "https://example.com/?url=x&code=y", "clip-pipeline://pair", "clip-pipeline://pair?code=abc",
            "clip-pipeline://pair?url=ftp%3A%2F%2Fx&code=abc", "clip-pipeline://other?url=http%3A%2F%2Fx&code=abc",
            "clip-pipeline://pair?url=192.168.1.20%3A8000&code=abc",
        )) assertNull(text, PairingLink.parse(text))
    }

    @Test
    fun aTypedAddressNeedsItsScheme() {
        assertTrue(PairingLink.isServerAddress("http://192.168.1.20:8000"))
        assertTrue(PairingLink.isServerAddress(" https://clips.example.test "))
        assertFalse(PairingLink.isServerAddress("192.168.1.20:8000"))
        assertFalse(PairingLink.isServerAddress("server"))
    }
}
