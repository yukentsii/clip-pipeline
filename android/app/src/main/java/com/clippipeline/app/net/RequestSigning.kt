package com.clippipeline.app.net

import java.security.KeyPair
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.Signature
import java.util.Base64
import java.util.Locale

/**
 * The request-signing protocol described at the top of `clip_pipeline/auth.py`. Every request carries
 * five headers; the signature covers the method, the path with its query exactly as sent, the time,
 * a one-time nonce and the hash of the body.
 */
object RequestSigning {
    const val HEADER_DEVICE = "X-Device"
    const val HEADER_TIMESTAMP = "X-Timestamp"
    const val HEADER_NONCE = "X-Nonce"
    const val HEADER_CONTENT_SHA256 = "X-Content-SHA256"
    const val HEADER_SIGNATURE = "X-Signature"

    const val EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    fun sha256Hex(bytes: ByteArray): String = hex(MessageDigest.getInstance("SHA-256").digest(bytes))

    fun hex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }

    fun signingString(method: String, pathAndQuery: String, timestamp: String, nonce: String, bodySha256: String): ByteArray =
        listOf(method.uppercase(Locale.ROOT), pathAndQuery, timestamp, nonce, bodySha256)
            .joinToString("\n").toByteArray(Charsets.UTF_8)

    /** 24 URL-safe characters, like Python's `secrets.token_urlsafe(18)`. */
    fun newNonce(random: SecureRandom): String {
        val bytes = ByteArray(18).also(random::nextBytes)
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }

    fun headers(
        signer: DeviceSigner,
        deviceId: String,
        method: String,
        pathAndQuery: String,
        bodySha256: String,
        timestamp: String,
        nonce: String,
    ): Map<String, String> {
        val signature = signer.sign(signingString(method, pathAndQuery, timestamp, nonce, bodySha256))
        return linkedMapOf(
            HEADER_DEVICE to deviceId,
            HEADER_TIMESTAMP to timestamp,
            HEADER_NONCE to nonce,
            HEADER_CONTENT_SHA256 to bodySha256,
            HEADER_SIGNATURE to Base64.getEncoder().encodeToString(signature),
        )
    }
}

/** Holds the device's private key and signs with it. The key itself is never handed out. */
interface DeviceSigner {
    /** Base64 of the X.509 SubjectPublicKeyInfo: what `POST /pair` takes as `public_key`. */
    val publicKeyBase64: String

    /** ECDSA-SHA256 over [message], DER encoded. */
    fun sign(message: ByteArray): ByteArray
}

class KeyPairSigner(private val keyPair: KeyPair) : DeviceSigner {
    override val publicKeyBase64: String
        get() = Base64.getEncoder().encodeToString(keyPair.public.encoded)

    override fun sign(message: ByteArray): ByteArray =
        Signature.getInstance("SHA256withECDSA").run {
            initSign(keyPair.private)
            update(message)
            sign()
        }
}
