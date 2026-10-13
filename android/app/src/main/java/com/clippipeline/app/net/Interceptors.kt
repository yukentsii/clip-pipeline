package com.clippipeline.app.net

import okhttp3.HttpUrl
import okhttp3.Interceptor
import okhttp3.Response
import okio.HashingSink
import okio.blackholeSink
import okio.buffer
import java.io.IOException
import java.security.SecureRandom

/** The server this phone is paired with (or is pairing with right now), and who we are to it. */
data class Server(val url: HttpUrl, val deviceId: String?)

class CleartextRefusedException(url: HttpUrl) :
    IOException("Refused to send a plain-HTTP request to ${url.host}:${url.port}: it is not the paired server")

/**
 * Plain HTTP is allowed to the paired server and to nothing else. The check runs before anything is
 * sent, and again on every redirect hop. HTTPS requests pass, so moving the server to HTTPS is only
 * a change of its URL.
 */
class CleartextGuard(private val server: () -> Server?) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val url = chain.request().url
        if (!url.isHttps) {
            val paired = server()?.url
            if (paired == null || url.host != paired.host || url.port != paired.port) throw CleartextRefusedException(url)
        }
        return chain.proceed(chain.request())
    }
}

/** Tag on a request whose body is too big to hash while sending: its SHA-256, worked out beforehand. */
class BodySha256(val hex: String)

/** Adds the five signed headers to every request made once the phone is paired. */
class SigningInterceptor(
    private val server: () -> Server?,
    private val signer: () -> DeviceSigner?,
    private val now: () -> Long = { System.currentTimeMillis() / 1000 },
    private val random: SecureRandom = SecureRandom(),
) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val deviceId = server()?.deviceId
        val signer = signer()
        if (deviceId == null || signer == null) return chain.proceed(request)      // pairing itself is unsigned

        val url = request.url
        val pathAndQuery = url.encodedPath + (url.encodedQuery?.let { "?$it" } ?: "")
        val bodyHash = request.tag(BodySha256::class.java)?.hex ?: request.body?.let { body ->
            val sink = HashingSink.sha256(blackholeSink())
            sink.buffer().use { body.writeTo(it) }
            sink.hash.hex()
        } ?: RequestSigning.EMPTY_SHA256
        val headers = RequestSigning.headers(
            signer, deviceId, request.method, pathAndQuery, bodyHash,
            timestamp = now().toString(), nonce = RequestSigning.newNonce(random),
        )
        val signed = request.newBuilder().apply { headers.forEach { (name, value) -> header(name, value) } }.build()
        return chain.proceed(signed)
    }
}
