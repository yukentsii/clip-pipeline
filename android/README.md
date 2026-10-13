# clip-pipeline for Android

The app that operates the clip-pipeline service: review and approve clips, add videos, see the
schedule, the status and the reach. Kotlin and Jetpack Compose; the design is the Figma file named in
[docs/PLAN.md](../docs/PLAN.md).

## Build

Needs the Android SDK (platform 37, build-tools 36) and a JDK that the Android Gradle plugin accepts;
Android Studio brings both.

```bash
./gradlew :app:assembleDebug        # the APK lands in app/build/outputs/apk/debug/
./gradlew :app:testDebugUnitTest    # unit tests
```

`local.properties` (the SDK's location) is made by Android Studio and is not kept in git.

## Use

1. On the server: `python -m clip_pipeline pair` (set `BIND_HOST` and `PUBLIC_URL` first, see the main README, "Access").
2. Open the app and point the camera at the code, or choose "Type the code instead".

On an emulator the camera cannot see your screen, so type the address and the code. If the emulator
cannot reach the server on `10.0.2.2`, forward the port and use `http://127.0.0.1:<port>` as the address:

```bash
adb reverse tcp:8000 tcp:8000
```

## How it talks to the server

* `net/RequestSigning.kt`, `net/Interceptors.kt`: every request is signed with the device key (five headers;
  the protocol is at the top of `clip_pipeline/auth.py`). The key is made in the Android Keystore at pairing.
* One OkHttp client (`ClipApp.http`) serves the API, the video player and the thumbnails. It refuses
  plain HTTP to anything but the paired server; HTTPS needs no change in the app.
* `app/src/test/resources/signing_vectors.json` was made by the `Phone` class in `tests/test_auth.py`.
  The key inside is a throwaway that exists only for that test.
