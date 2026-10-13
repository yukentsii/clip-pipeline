package com.clippipeline.app

import com.clippipeline.app.net.BodySha256
import com.clippipeline.app.net.CleartextGuard
import com.clippipeline.app.net.CleartextRefusedException
import com.clippipeline.app.net.KeyPairSigner
import com.clippipeline.app.net.RequestSigning
import com.clippipeline.app.net.Server
import com.clippipeline.app.net.SigningInterceptor
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64

class InterceptorsTest {
    private val paired = MockWebServer()
    private val other = MockWebServer()
    private val keyPair = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
    private val signer = KeyPairSigner(keyPair)
    private var server: Server? = null

    private fun client(vararg extra: Interceptor): OkHttpClient {
        val guard = CleartextGuard { server }
        return OkHttpClient.Builder()
            .addInterceptor(guard)
            .addInterceptor(SigningInterceptor({ server }, { signer }, now = { 1791840000L }))
            .apply { extra.forEach(::addInterceptor) }
            .addNetworkInterceptor(guard)
            .build()
    }

    private fun ok() = MockResponse.Builder().body("ok").build()

    @Before
    fun start() {
        paired.start()
        other.start()
        server = Server(paired.url("/"), "device-1")
    }

    @After
    fun stop() {
        paired.close()
        other.close()
    }

    // ------------------------------------------------------------ plain HTTP only to the paired server

    @Test
    fun plainHttpToAnotherHostIsRefusedBeforeAnythingIsSent() {
        other.enqueue(ok())
        try {
            client().newCall(Request.Builder().url(other.url("/status")).build()).execute()
            fail("the request should have been refused")
        } catch (e: CleartextRefusedException) {
            assertTrue(e.message!!.contains("not the paired server"))
        }
        assertEquals("nothing reached the other server", 0, other.requestCount)
    }

    @Test
    fun plainHttpToThePairedHostGoesThrough() {
        paired.enqueue(ok())
        client().newCall(Request.Builder().url(paired.url("/status")).build()).execute().use {
            assertEquals(200, it.code)
        }
        assertEquals(1, paired.requestCount)
    }

    @Test
    fun plainHttpIsRefusedWhenThePhoneIsNotPaired() {
        server = null
        paired.enqueue(ok())
        try {
            client().newCall(Request.Builder().url(paired.url("/status")).build()).execute()
            fail("the request should have been refused")
        } catch (_: CleartextRefusedException) {
        }
        assertEquals(0, paired.requestCount)
    }

    @Test
    fun aRedirectToAnotherPlainHttpHostIsNotFollowed() {
        paired.enqueue(MockResponse.Builder().code(302).addHeader("Location", other.url("/elsewhere").toString()).build())
        other.enqueue(ok())
        try {
            client().newCall(Request.Builder().url(paired.url("/status")).build()).execute()
            fail("the redirect should have been refused")
        } catch (_: CleartextRefusedException) {
        }
        assertEquals(0, other.requestCount)
    }

    @Test
    fun httpsPassesTheCheckSoMovingToHttpsIsOnlyAChangeOfUrl() {
        // The last interceptor answers instead of the network: the guard let the request through to it.
        val answered = Interceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(200).message("OK")
                .body("ok".toResponseBody(null)).build()
        }
        server = Server("https://clips.example.test/".toHttpUrl(), "device-1")
        client(answered).newCall(Request.Builder().url("https://clips.example.test/status").build()).execute().use {
            assertEquals(200, it.code)
            assertEquals("device-1", it.request.header("X-Device"))     // and it is signed exactly the same way
        }
    }

    // ------------------------------------------------------------ the five signed headers

    private fun verifies(signatureBase64: String, message: ByteArray): Boolean =
        Signature.getInstance("SHA256withECDSA").run {
            initVerify(keyPair.public)
            update(message)
            verify(Base64.getDecoder().decode(signatureBase64))
        }

    @Test
    fun everyRequestCarriesTheFiveHeadersOverWhatWasActuallySent() {
        paired.enqueue(ok())
        paired.enqueue(ok())
        val http = client()
        http.newCall(Request.Builder().url(paired.url("/clips?status=review&limit=5")).build()).execute().close()
        val body = """{"review_min_score": 80}"""
        http.newCall(
            Request.Builder().url(paired.url("/settings")).patch(body.toRequestBody("application/json".toMediaType())).build()
        ).execute().close()

        val get = paired.takeRequest()
        val patch = paired.takeRequest()
        for ((request, sentBody) in listOf(get to "", patch to body)) {
            val h = request.headers
            assertEquals("device-1", h["X-Device"])
            assertEquals("1791840000", h["X-Timestamp"])
            assertTrue(Regex("^[A-Za-z0-9_\\-]{16,64}$").matches(h["X-Nonce"]!!))
            assertEquals(RequestSigning.sha256Hex(sentBody.toByteArray()), h["X-Content-SHA256"])
            val pathAndQuery = request.url.encodedPath + (request.url.encodedQuery?.let { "?$it" } ?: "")
            val message = RequestSigning.signingString(request.method, pathAndQuery, h["X-Timestamp"]!!, h["X-Nonce"]!!, h["X-Content-SHA256"]!!)
            assertTrue("signature of ${request.method} $pathAndQuery", verifies(h["X-Signature"]!!, message))
        }
        assertEquals("/clips?status=review&limit=5", get.url.encodedPath + "?" + get.url.encodedQuery)
        assertTrue(get.headers["X-Nonce"] != patch.headers["X-Nonce"])
    }

    @Test
    fun aStreamedUploadIsSignedWithTheHashWorkedOutBeforehand() {
        paired.enqueue(ok())
        val video = ByteArray(100_000) { (it % 251).toByte() }
        val hash = RequestSigning.sha256Hex(video)
        val request = Request.Builder().url(paired.url("/sources/upload/My%20Clip.mp4?content_type=film"))
            .post(video.toRequestBody(null)).tag(BodySha256::class.java, BodySha256(hash)).build()
        client().newCall(request).execute().close()
        val sent = paired.takeRequest()
        assertEquals(hash, sent.headers["X-Content-SHA256"])
        assertEquals("/sources/upload/My%20Clip.mp4", sent.url.encodedPath)
    }

    @Test
    fun pairingItselfIsNotSigned() {
        server = Server(paired.url("/"), deviceId = null)
        paired.enqueue(ok())
        client().newCall(Request.Builder().url(paired.url("/pair")).post("{}".toRequestBody(null)).build()).execute().close()
        assertNull(paired.takeRequest().headers["X-Signature"])
    }
}
