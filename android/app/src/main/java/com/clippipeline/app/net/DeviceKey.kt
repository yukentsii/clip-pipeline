package com.clippipeline.app.net

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.spec.ECGenParameterSpec

/** The device's ECDSA P-256 key. It is made in the Android Keystore and the private half never leaves it. */
object DeviceKey {
    private const val STORE = "AndroidKeyStore"
    private const val ALIAS = "clip-pipeline-device"

    private fun store(): KeyStore = KeyStore.getInstance(STORE).apply { load(null) }

    /** A fresh key for a new pairing; replaces any earlier one. */
    fun create(): DeviceSigner {
        store().deleteEntry(ALIAS)
        val spec = KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)
            .build()
        val pair = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, STORE).run {
            initialize(spec)
            generateKeyPair()
        }
        return KeyPairSigner(pair)
    }

    fun load(): DeviceSigner? {
        val store = store()
        val private = store.getKey(ALIAS, null) as? PrivateKey ?: return null
        val public = store.getCertificate(ALIAS)?.publicKey ?: return null
        return KeyPairSigner(KeyPair(public, private))
    }

    fun delete() = store().deleteEntry(ALIAS)
}
