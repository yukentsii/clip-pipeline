package com.clippipeline.app

import android.app.Application
import com.clippipeline.app.data.Api
import com.clippipeline.app.data.Intake
import com.clippipeline.app.data.Notifier
import com.clippipeline.app.data.Session
import com.clippipeline.app.net.CleartextGuard
import com.clippipeline.app.net.DeviceKey
import com.clippipeline.app.net.DeviceSigner
import com.clippipeline.app.net.SigningInterceptor
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

class ClipApp : Application() {
    lateinit var session: Session
        private set

    @Volatile
    var signer: DeviceSigner? = null

    /**
     * The only HTTP client in the app. The API, the video player and thumbnails all use it, so every
     * request passes the cleartext check and is signed.
     */
    lateinit var http: OkHttpClient
        private set

    lateinit var api: Api
        private set

    lateinit var intake: Intake
        private set

    override fun onCreate() {
        super.onCreate()
        session = Session(this)
        runBlocking { session.load() }
        signer = runCatching { DeviceKey.load() }.getOrNull()
        val guard = CleartextGuard { session.current }
        http = OkHttpClient.Builder()
            .addInterceptor(guard)
            .addInterceptor(SigningInterceptor({ session.current }, { signer }))
            .addNetworkInterceptor(guard)            // redirects are checked too
            .readTimeout(60, TimeUnit.SECONDS)
            .build()
        api = Api(http) { session.current }
        intake = Intake(this)
        Notifier.schedule(this)
    }
}
