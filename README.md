# clip-pipeline

Turns long videos into short captioned clips and posts them on a schedule. One self-hosted service,
no paid APIs: yt-dlp, faster-whisper, the Gemini free tier (optional), MediaPipe and ffmpeg.

> The backend is complete; the Android app that operates it comes next.
> See [docs/PLAN.md](docs/PLAN.md) for the plan and what is done.

```
link or local video ─► audio only ─► loudness / chat peaks ─► Whisper on the candidate minutes
    ─► Gemini scoring (falls through a list of models, then to an offline heuristic)
    ─► clips above the review score are cut and wait for approval
    ─► approve ─► face-aware framing + captions + hook title + loudness ─► scheduled post
    ─► YouTube / Instagram / TikTok
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt     # Linux/macOS: .venv/bin/python
copy .env.example .env                                       # then fill in GEMINI_API_KEY and platform logins
```

ffmpeg must be on `PATH` (Windows: `winget install Gyan.FFmpeg`; Debian/Ubuntu: `apt install ffmpeg`). For TikTok
uploads also run `python -m playwright install chromium`.

Rendering uses the first hardware encoder that really works on the machine (NVIDIA NVENC, Intel Quick Sync,
AMD AMF, or VAAPI on Linux) and falls back to software encoding, so it runs with or without a GPU. Set
`VIDEO_ENCODER` to force one.

### Docker

```bash
copy .env.example .env        # fill in; leave DATA_DIR, BIND_HOST and PORT to the image
docker compose up -d --build
docker compose exec clip-pipeline python -m clip_pipeline pair
```

One image and one volume: everything the service keeps is in `./data` on the host, and videos dropped into
`./data/inbox` are picked up. The port is published on `127.0.0.1` only; `compose.yaml` shows what to change to
reach it from a phone on your private network, and how to pass a GPU in for hardware encoding. Build with
`--build-arg WITH_TIKTOK=false` to leave out the browser TikTok needs.

## Use

```bash
python -m clip_pipeline serve                       # run the service (API + worker + scheduler)
python -m clip_pipeline add <link-or-video-file>    # queue a source; add --type film|podcast|gaming|…
python -m clip_pipeline run                         # work through the queue without the service, then exit
python -m clip_pipeline status                      # sources, clips and posts
```

While the service runs, there are three ways to give it a video. All of them start processing by themselves:

* **Inbox folder**: copy a video into `DATA_DIR/inbox` (or `INBOX_DIR`). It is taken once the copy has finished.
* **Link**: `POST /sources` with `{"url": "…"}`.
* **Upload**: `POST /sources/upload/<filename>` with the video as the raw request body.

`GET /sources` and `GET /sources/<id>` show progress. A video that was already processed is recognised
(by content for files, by video id for links) and not processed again; a repeated inbox file is moved to
`inbox/_duplicates`. Files that arrive through the inbox or an upload are deleted once every clip from them has
been approved or rejected; a file added by path with `add` is never touched.

A source goes through six stages (audio, analysis, Whisper, scoring, slices, render). Each stage saves its
result before the next starts, so a restart resumes where it stopped. Every clip that reaches the review
score is rendered before review, so what you approve is exactly what gets posted.

## API

Interactive docs are at `/docs` when `AUTH_REQUIRED=false` (development).

| | |
|---|---|
| `POST /sources`, `POST /sources/upload/<name>` | send a link or a video |
| `GET /sources`, `GET /sources/<id>`, `POST /sources/<id>/retry` | progress; retry a failed source |
| `GET /clips` | the review feed (finished clips waiting for a decision, best first) |
| `GET /clips/<id>/video`, `/thumb` | the rendered clip and its thumbnail |
| `PATCH /clips/<id>` | edit post text (instant) or trim / framing / captions / hook title (re-renders) |
| `POST /clips/<id>/approve`, `/reject`, `/retry` | schedule it to chosen accounts (each at its next free slot, or a given time), drop it, or retry |
| `GET /posts`, `PATCH /posts/<id>`, `DELETE /posts/<id>` | the schedule; change or cancel what has not gone out |
| `GET /accounts`, `POST /accounts`, `PATCH /accounts/<id>` | where clips go: several accounts per platform, each with its own login and daily posting slots |
| `GET /accounts/<id>/next-slots` | that account's next free posting times |
| `GET /events` | what happened (clips ready, uploads, failures), for notifications |
| `GET /insights` | reach: totals, daily curve, week against week, best clips, what works |
| `GET /status` | what is running, counts, and everything that needs attention with its reason |
| `GET /settings`, `PATCH /settings` | review score and render defaults |

| `POST /pair`, `GET /devices`, `DELETE /devices/<id>` | pair a phone; list and revoke paired phones |

## Access

Only paired devices can use the API. `/health` and `/pair` are the only endpoints open to anyone who can reach
the address, and the interactive docs are served only when `AUTH_REQUIRED=false`.

```bash
python -m clip_pipeline pair              # shows a one-time code (and a QR code) valid for 10 minutes
python -m clip_pipeline devices           # paired phones
python -m clip_pipeline devices revoke <id>
```

The phone creates an ECDSA P-256 key in its hardware keystore and registers the public half with the code.
The private key never leaves the phone and the server stores nothing secret. Every request is then signed
over its method, path, time, a one-time nonce and the hash of its body, so it cannot be replayed or altered;
uploads are hashed as they arrive and thrown away if the bytes are not the ones that were signed. Failed
requests and pairing attempts are rate limited per address. The exact protocol is described at the top of
`clip_pipeline/auth.py`.

The service listens on `127.0.0.1` unless `BIND_HOST` says otherwise. It speaks plain HTTP: put it on a
private network, or behind a TLS reverse proxy before exposing it any further.

Everything the service writes lives under `DATA_DIR` (default `./data`): the SQLite database, each
source's working files, the inbox folder and saved logins. None of it belongs in version control.

## Accounts and reach

Logins in `.env` become an account called `default` on first start; add more with `POST /accounts` or import an
env file with `python -m clip_pipeline accounts import <file>`. Credentials are stored in the database under
`DATA_DIR` and are never returned by the API.

Once a clip is posted its numbers are read a few times a day while it is fresh, then less often, and every
reading is kept. YouTube needs a login with read access: `python -m clip_pipeline accounts login <account>`
logs an existing account in again in the browser and saves the result. Instagram needs the
`instagram_manage_insights` permission on its token for view counts. TikTok has no numbers yet.

## Platform logins

* **YouTube**: create a Desktop OAuth client with YouTube Data API v3 enabled, run
  `python -m clip_pipeline.publish.scripts.youtube_auth client_secret.json` and put the printed values in `.env`.
* **Instagram**: Creator/Business account linked to a Facebook Page; set `INSTAGRAM_USER_ID` and a
  long-lived `INSTAGRAM_ACCESS_TOKEN`.
* **TikTok**: `python -m clip_pipeline.publish.uploaders.tiktok --login --state <file>` once on a machine
  with a screen. If TikTok later shows a login wall or CAPTCHA the post fails until you log in again;
  the uploader never tries to get past one.

`python -m clip_pipeline.publish.scripts.preflight clip.mp4` exercises every configured uploader
without publishing anything.

## Tests

```bash
python -m pytest
```

## Licence

[MIT](LICENSE).
