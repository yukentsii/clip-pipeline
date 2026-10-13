package com.clippipeline.app

import com.clippipeline.app.net.KeyPairSigner
import com.clippipeline.app.net.RequestSigning
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.security.KeyFactory
import java.security.KeyPair
import java.security.PublicKey
import java.security.Signature
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec
import java.util.Base64

/**
 * The Kotlin signing code against `signing_vectors.json`, which was produced by the `Phone` class in
 * the backend's `tests/test_auth.py` (the reference client) for fixed inputs.
 *
 * ECDSA signatures are random, so two signatures of the same message never match byte for byte.
 * What must match is everything else: the four deterministic headers, and the exact bytes that are
 * signed. The second is proven by checking Phone's signature against the message Kotlin builds.
 */
class SigningVectorsTest {
    private val vectors: JsonObject = Json.parseToJsonElement(
        checkNotNull(javaClass.getResourceAsStream("/signing_vectors.json")).reader().readText()
    ).jsonObject

    private fun text(o: JsonObject, key: String) = o.getValue(key).jsonPrimitive.content

    private val ec = KeyFactory.getInstance("EC")
    private val publicKey: PublicKey =
        ec.generatePublic(X509EncodedKeySpec(Base64.getDecoder().decode(text(vectors, "public_key_base64"))))
    private val signer = KeyPairSigner(
        KeyPair(publicKey, ec.generatePrivate(PKCS8EncodedKeySpec(Base64.getDecoder().decode(text(vectors, "private_key_pkcs8_base64")))))
    )

    private fun verifies(signatureBase64: String, message: ByteArray): Boolean =
        Signature.getInstance("SHA256withECDSA").run {
            initVerify(publicKey)
            update(message)
            verify(Base64.getDecoder().decode(signatureBase64))
        }

    @Test
    fun publicKeyIsEncodedTheWayTheServerExpects() {
        assertEquals(text(vectors, "public_key_base64"), signer.publicKeyBase64)
    }

    @Test
    fun headersMatchTheReferenceClient() {
        val deviceId = text(vectors, "device_id")
        val cases = vectors.getValue("cases").jsonArray.map { it.jsonObject }
        assertTrue(cases.size >= 6)
        val kotlinSigned = buildJsonArray {
            for (case in cases) {
                val method = text(case, "method")
                val path = text(case, "path")
                val body = Base64.getDecoder().decode(text(case, "body_base64"))
                val timestamp = case.getValue("timestamp").jsonPrimitive.int.toString()
                val nonce = text(case, "nonce")
                val expected = case.getValue("headers").jsonObject

                val bodyHash = RequestSigning.sha256Hex(body)
                val headers = RequestSigning.headers(signer, deviceId, method, path, bodyHash, timestamp, nonce)

                assertEquals(setOf("X-Device", "X-Timestamp", "X-Nonce", "X-Content-SHA256", "X-Signature"), headers.keys)
                for (name in listOf("X-Device", "X-Timestamp", "X-Nonce", "X-Content-SHA256")) {
                    assertEquals("$name of $method $path", text(expected, name), headers[name])
                }
                // Phone's signature is valid for the message Kotlin builds: the signed bytes are identical.
                val message = RequestSigning.signingString(method, path, timestamp, nonce, bodyHash)
                assertTrue("Phone's signature over Kotlin's message, $method $path", verifies(text(expected, "X-Signature"), message))
                // And Kotlin's own signature is a valid DER ECDSA-SHA256 signature by the same key.
                assertTrue("Kotlin's signature, $method $path", verifies(headers.getValue("X-Signature"), message))

                add(buildJsonObject {
                    put("method", method); put("path", path); put("body_base64", text(case, "body_base64"))
                    put("headers", buildJsonObject { headers.forEach { (k, v) -> put(k, v) } })
                })
            }
        }
        // Left for the reverse check: the backend's own verifier accepts these (see docs/PLAN.md).
        File("build/signing").apply { mkdirs() }.resolve("kotlin_signed.json").writeText(
            buildJsonObject { put("public_key_base64", signer.publicKeyBase64); put("cases", kotlinSigned) }.toString()
        )
    }

    @Test
    fun emptyBodyHashIsTheHashOfNothing() {
        assertEquals(RequestSigning.EMPTY_SHA256, RequestSigning.sha256Hex(ByteArray(0)))
    }

    @Test
    fun noncesAreTwentyFourUrlSafeCharacters() {
        val seen = (1..200).map { RequestSigning.newNonce(java.security.SecureRandom()) }
        assertTrue(seen.all { Regex("^[A-Za-z0-9_\\-]{24}$").matches(it) })
        assertEquals(200, seen.toSet().size)
    }
}
