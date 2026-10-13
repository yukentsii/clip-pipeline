# clip-pipeline: work plan

Working document for the rebuild. Decisions here were made by the project owner on 2026-10-12;
change them only with their say-so. Tick steps off as they land.

## What this is

One backend service that turns long videos into short captioned clips and posts them on a schedule,
operated from a minimal Android app. Runs in Docker on a home server (never cloud or paid services).
Built and tested on a Windows laptop first.

```
shared folder / link from the app
        │  (starts by itself, one source at a time, low priority)
        ▼
 audio ─► loudness peaks ─► Whisper on candidates ─► Gemini scoring (model fallback chain ─► local heuristic)
        ▼
 clips scoring ≥ 70 are cut and wait for review (forever)
        ▼
 Android app: play, edit post text, pick platforms + accounts, approve / reject
        ▼
 render (face-aware framing, captions, hook title, loudness) ─► next free posting slot per account
        ▼
 YouTube / Instagram / TikTok ─► push notification on every upload and every failure
```

## Decisions

| Area | Decision |
|---|---|
| Shape | One service, one SQLite database, one settings file. No HTTP hop between "worker" and "publisher". |
| Clients | Native Android app (Kotlin, designed in Figma) built after the backend. No web page. |
| Getting videos in | Shared folder on the server (the owner's `Z:` drive) for PC files; links/videos from the app. |
| Start | Every provided source starts processing automatically. Never ask to confirm a source. |
| Review | Nothing posts without approval in the app. Clips scoring ≥ 70 are offered, no cap per source. |
| At approval | Post text (title, description, tags) editable; platforms and accounts chosen per clip. |
| Unreviewed clips | Wait forever. |
| Scheduling | Approved clips fill the next free slot. Default 3 slots/day per account. Times editable, per platform. Scheduled posts can be cancelled. No takedowns from platforms. |
| Accounts | Several per platform. |
| Post text language | Same as the video. |
| Notifications | Built into the app (Firebase push): every upload and every failure. |
| Security | Each phone pairs by QR code and signs its requests with a device key, plain HTTP on the private network for now; HTTPS in front of it before it is exposed. Private network only until the project is finished. |
| Housekeeping | Rendered file deleted once posted everywhere. Source file deleted once all its clips are decided. Dates shown day/month/year. |
| Heavy work | Any time, one job at a time, low CPU priority. |
| Style | 10–20 rating rounds with the owner, then defaults are tuned. |
| Old test data | New database starts empty; the old `data/jobs` folder is left alone. |
| Name | `clip-pipeline` (repo, Docker image, Android app). |

## Layout

```
clip_pipeline/
  config.py        one Settings object, from env / .env
  db.py            SQLite connection + versioned schema migrations
  store.py         sources and clips (typed reads/writes)
  worker.py        resumable processing: source ─► clips ─► render ─► post
  hardware.py      ffmpeg / encoder detection
  auth.py          paired devices, signed requests, rate limits
  proc.py          run below normal CPU priority
  sources/         downloader.py (yt-dlp), local.py (files on disk), inbox.py (watcher, fingerprints)
  analysis/        audio_analyzer.py, transcriber.py, llm_scorer.py
  render/          face_tracker.py, subtitle_gen.py, video_renderer.py
  publish/         queue.py (posts), accounts.py, scheduler.py, stats.py (reach), uploaders/, scripts/
  api/             app.py (FastAPI), sources.py, clips.py, posts.py, accounts.py, status.py
  __main__.py      CLI: serve | add | status
tests/
docs/PLAN.md
```

Data directory (`DATA_DIR`, default `./data`): `clip_pipeline.db`, `sources/<id>/…`, `inbox/`, `secrets/`.

## Steps

Each step ends with the test suite green and a real run where possible.

### 1. One service, one database  ✓ done 2026-10-12
- [x] New `clip_pipeline` package; move the working modules in unchanged apart from imports.
- [x] One `Settings` replacing `app.core.config` and `server.config`.
- [x] `db.py` with migrations; `store.py` for sources and clips (replaces `job.json` + in-memory state).
- [x] Post queue in the same database (`posts`, `post_targets`), ported from `server/database.py`.
- [x] `worker.py`: stages resume from saved artifacts after a restart instead of failing.
- [x] Approving a clip renders it and writes the post directly (no upload to a second service).
- [x] Minimal API (`/health`) and a CLI for testing; real API comes in step 3.
- [x] Port every existing test; add store, worker-resume and approve-to-post tests.
- [x] Remove `app/`, `server/`, the web page and the `.bat` launcher; one `requirements.txt`.

### 2. Sources  ✓ done 2026-10-12
- [x] Inbox folder watcher (waits until a copy has finished before picking a file up).
- [x] Links and uploads through the API. Everything auto-starts.
- [x] One source at a time; heavy subprocesses and Whisper at low priority.
- [x] No repeats: remember processed sources; skip clips that overlap one already made.
- [x] Source file removed once all its clips are approved or rejected.

### 3. The app's API  ✓ done 2026-10-12
- [x] Review feed: clips ≥ threshold with a playable preview, scores, post text.
- [x] Approve (with edits, platforms, accounts), reject, re-trim.
- [x] Schedule: list, edit time/text/platforms while pending, cancel.
- [x] Status: sources in progress, failures with reasons, per-account health (e.g. TikTok login expired).
- [x] Settings: threshold, slots, defaults per content type.
- Decided: review shows the finished clip, so every clip above the score is rendered before review;
  approving is instant and a picture edit re-renders. Accounts and auto slots at approval come in step 4.

### 4. Publishing  ✓ done 2026-10-13
- [x] `accounts` table: several per platform, each with credentials and posting slots.
- [x] Uploaders take an account instead of global settings.
- [x] Auto slots (default 3/day per account), per-platform times on each target.
- [x] Delete the rendered file once every target has finished.
- [x] Events table feeding notifications (upload done, upload failed, clips ready).
- [x] Reach: readings of every posted clip kept over time (`target_stats`, `account_stats`), `/insights`.
- Done 2026-10-13: the laptop's YouTube account was logged in again with read access
  (`python -m clip_pipeline accounts login <account>`) and its Instagram token now carries
  `instagram_manage_insights` (added to the Meta app, long-lived token saved); both checked live.
- Open: TikTok has no numbers because posting through the browser returns no video id.
- Decided: the owner picks accounts per clip at approval; a target without a time takes that account's
  next free slot. "Gain" in Insights means reach only, not money.

### 5. Device pairing  ✓ done 2026-10-13
- [x] QR pairing; device public keys; signed requests; revoke a device.
- [x] Rate limiting. Bind to the private network by default.
- Keys are ECDSA P-256 (what every Android hardware keystore supports). The protocol is described at the
  top of `clip_pipeline/auth.py`; `tests/test_auth.py` contains a complete reference client.
- Open: the QR code needs the optional `segno` package (not installed yet); without it `pair` prints the
  link and code as text. Replay protection is kept in memory, so it restarts with the service (requests
  older than five minutes are refused regardless).

### 6. Docker and GitHub readiness  ✓ done 2026-10-13
- [x] Dockerfile + compose with one data volume; hardware encoder detection (NVENC, Quick Sync, AMF, VAAPI)
      with software fallback; Liberation Sans installed in the image so captions match Arial.
- [x] Secrets never in the repo (`.env`, `client_secret.json`, anything under `data/`); checked by listing
      what git would upload (69 files) and scanning them.
- [x] README, CI running the tests on Linux.
- [x] Licence: MIT, held by "clip-pipeline contributors".
- Not verified yet (the owner chose to wait for the first GitHub push): the image has not been built and
  the tests have not been run on Linux (no Docker on the
  development laptop). VAAPI, NVENC and Quick Sync could only be tested as far as building their commands;
  on the laptop only AMF passes its test encode.

### Fixes after the first real phone test (2026-10-13)
- [x] A video that arrives through the inbox or an upload keeps the name it arrived with. It used to become
      "original" once processing started, because the title was read back from the moved file's name. The
      title now only changes if the video carries one in its own metadata.
- [x] A Gemini request that gets no reply can no longer hold up the queue. Every request has a time limit
      (`GEMINI_TIMEOUT_SECONDS`, default 120); an unanswered one is tried once more, then the next model is
      used, and after `GEMINI_BATCH_BUDGET_SECONDS` (default 480) the batch is scored offline. The source's
      message says which model is being tried. Checked against the real API with a 1 ms limit.
- [x] Caption language is no longer guessed window by window (a song at the start of a window could flip a
      whole clip's captions). The languages that carry at least `LANGUAGE_MIN_SHARE` (15%) of a video's speech
      are found first; with one such language everything is transcribed in it, with several each stretch of
      speech between pauses takes the likeliest of them. `WHISPER_LANGUAGE` still forces one language.
      Measured on a real English video: 29.8 s against 23.5 s with the language forced, same 420 words.
      Not tested on a real two-language video.
- [x] Whisper model on the laptop changed from `small` to `large-v3-turbo` (`WHISPER_MODEL` in `.env`). The
      small model heard an English video spoken with a Swedish accent as "sv 64%, en 33%"; the same video
      with the new model: "en 100%". Measured cost on the laptop's CPU: 78.9 s against 29.8 s for the same
      four minutes of audio (about 2.6 times slower), with 449 words caught against 420. Whisper runs on the
      CPU here because the engine only supports NVIDIA GPUs; on a server with one, set `WHISPER_DEVICE=cuda`.
### After the backend
- Android app: Approve, Add, Status and Insights screens; share target; push notifications.
  The UI is designed in Figma first and the owner approves it (commenting on elements there) before
  anything is built from it.
  Design direction: dark and minimal; a slightly warm white as the only accent; Review is a list of cards
  (thumbnail and score) that open a clip, not a full-screen feed; four tabs (Review, Add, Schedule, Insights);
  Status is an icon with a badge at the top; pairing and settings are separate screens.
  Design approved by the owner on 2026-10-13: https://www.figma.com/design/pfs4cVFbrAbCoWf22lzhwT (page
  "Screens", frames 1 Review … 8 Settings). The top bar of each tab screen has a settings gear and a bell
  side by side; the bell shows a dot, never a number, when something needs attention. Icons in the file
  are placeholders: use Material Symbols in the build.

  The app (in `android/`; built 2026-10-13, each step run on an emulator against the real backend):
  1. [x] Project, pairing and signing. Kotlin, Jetpack Compose, Material 3 dark theme with the file's colours
     (background 0C0D0E, cards 15171A, text and accent F3EFE6, muted 8C8980, green 7ED9A0, red E88080, amber
     E9C46A), the Inter font, min SDK 26. CameraX + ML Kit scan the `clip-pipeline://pair?url=…&code=…` link, or
     the address and code are typed; an ECDSA P-256 key is made in the Android Keystore; `POST /pair`; an OkHttp
     interceptor adds the five signed headers to every request (the protocol is at the top of
     `clip_pipeline/auth.py`; `tests/test_auth.py` has a complete reference client).
  2. [x] Screens, each against the real API: Review list (`GET /clips`) → Clip detail (Media3 player on
     `/clips/<id>/video`, `PATCH /clips/<id>`, approve with `targets`, reject) → Add (`POST /sources`, streamed
     `POST /sources/upload/<name>` with the body hash signed up front, share target) → Schedule (`/posts`, with a
     screen to edit or cancel a post) → Status (`/status`, `/events`) → Settings (`/accounts`, `/settings`,
     `/devices`) → Insights (`/insights`, charts drawn in Compose).
  3. [x] Notifications: a WorkManager job asks `GET /events?after=<last id>` every 15 minutes (the shortest
     interval Android allows), and the open app asks every 30 seconds. Still to do: move to Firebase push (the
     backend then needs a small sender for the events table).
  4. The backend must be reachable from the phone: set `BIND_HOST` and `PUBLIC_URL` (see README, "Access").

  Decided while building the app (the owner said to decide and keep the theme):
  - Plain HTTP: Android's network security config cannot name a host chosen at run time, so it permits plain
    HTTP and the app enforces the rule itself. One OkHttp client serves the API, the video player and the
    thumbnails; it refuses any plain-HTTP request whose host and port are not the paired server's, before
    anything is sent and on every redirect. HTTPS passes, so moving to HTTPS is only a change of the server URL.
    No server address is written in the repository.
  - Signing test: ECDSA signatures are random, so the Kotlin test checks the four other headers byte for byte
    against vectors made by the `Phone` class of `tests/test_auth.py`, and that Phone's signature is valid for
    the message Kotlin builds. The reverse was checked once with the backend's own verifier.
  - Pairing states the design lacked were drawn in Figma first: frames 7b (type the address and code), 7c
    (camera not allowed), 7d (in progress), 7e (failed, with the server's own message). A typed address needs
    its `http://` or `https://`; the scheme is never guessed.
  - States no frame shows: loading is a line of muted text, a failure is the reason in red with "Try again",
    an empty list says what will appear there.
  - Icons are Material Symbols (rounded); the font file is Inter (SIL Open Font License, `android/app/INTER-LICENSE.txt`).
  - Clip detail: text edits are saved when a field is left; tapping a tag removes it; a change to the on-screen
    title or the framing re-renders and the screen waits for it. Accounts without a login cannot be ticked.
    If a phone's hardware decoder fails on a clip the player retries once with software decoders.
  - Schedule: a post is "upcoming" while any of its targets has not gone out. The screen behind "Tap to edit
    text, time or accounts" has no frame; it reuses the Clip detail parts, with "Cancel post" and "Save".
  - Settings: an account unfolds to edit its posting times and switch it off; "Add an account" makes one
    without a login (logins are added on the server); the three defaults unfold to a choice; "Address" unfolds
    to "Unpair this phone". "Video encoder" and "Version" show a value and do nothing when tapped.
  - If the server answers that the phone is revoked or unknown, the app forgets the server and returns to pairing.
  - The bell's dot is on while `GET /status` lists anything under `needs_attention`.
  Verified on the owner's phone (Galaxy S25 Edge, Android 16) on 2026-10-13: installing the app, pairing by
  scanning the QR code, and signed requests to the backend over a real network.

  Decided 2026-10-13: until the new server arrives the laptop is the server, reached over Tailscale. `.env` sets
  `BIND_HOST` to the laptop's Tailscale address and `PUBLIC_URL` to `http://<that address>:8000`, so the service
  listens only inside the owner's Tailscale network (no port forwarding, nothing on the Wi-Fi). The laptop has
  to be on and Tailscale has to be on in the phone. Moving to the server later: copy `data/` and `.env`, set
  the new address, then unpair and pair the phone once.

  Not verified yet:
  - Sharing a video from another app's share sheet: on the emulator the app received the share and opened Add,
    but a share faked from the command line carries no permission to read the file. The same upload was
    verified through "Choose a video from this phone".
  - Playback with the phone's own hardware decoder.
  - Adding a working link, and real uploads to YouTube, Instagram or TikTok: the emulator tests ran on throwaway
    data with no platform logins and no Gemini key.
  - Insights was checked against made-up view readings, not real ones.
- Rating rounds to tune the editing style.
- Restore the policy pages on the new server (backup is outside the repo).

## Working rules
- Ask the owner before each step; approval for one step does not cover the next.
- Keep the tested analysis/render/upload code; rewrite a part only when it is clearly better, and ask first.
- Never print secret values. Backups of the old deployment live outside the repo.
