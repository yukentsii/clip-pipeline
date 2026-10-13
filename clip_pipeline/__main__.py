"""Command line.

    python -m clip_pipeline serve [--host HOST] [--port PORT]         run the service (default: BIND_HOST, PORT)
    python -m clip_pipeline pair                                      show a one-time code (QR) to pair a phone
    python -m clip_pipeline devices [revoke <id>]                     list paired phones, or cut one off
    python -m clip_pipeline add <url-or-video-file> [--type film]     queue a source
    python -m clip_pipeline run                                       work through the queue here, then exit
    python -m clip_pipeline status                                    sources, clips and posts
    python -m clip_pipeline accounts                                  list accounts (never shows logins)
    python -m clip_pipeline accounts import [path-to-.env]            make accounts from the logins in an env file

`add` only writes to the database; a running service picks the source up within seconds. Use `run`
to process the queue without the service (don't use both at once).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from clip_pipeline.config import get_settings
from clip_pipeline.db import Database
from clip_pipeline.publish.queue import PostQueue
from clip_pipeline.store import Store
from clip_pipeline.worker import Worker


def _parts():
    s = get_settings()
    db = Database(s.db_path)
    store = Store(db, s.sources_dir)
    queue = PostQueue(db)
    return s, store, queue, Worker(store, queue, s)


def cmd_serve(args) -> int:
    import uvicorn

    from clip_pipeline.api.app import create_app
    from clip_pipeline.proc import lower_process_priority

    s = get_settings()
    if s.low_priority:
        lower_process_priority()
    uvicorn.run(create_app(), host=args.host or s.bind_host, port=args.port or s.port)
    return 0


def cmd_add(args) -> int:
    from clip_pipeline.analysis.transcriber import CONTENT_TYPES
    from clip_pipeline.sources.downloader import InvalidURLError
    from clip_pipeline.sources.local import VIDEO_EXTS

    if args.type and args.type not in CONTENT_TYPES:
        print(f"unknown type {args.type!r}; use one of {', '.join(CONTENT_TYPES)}", file=sys.stderr)
        return 2
    _, _, _, worker = _parts()
    worker.kick = lambda: None   # only record it; `serve` or `run` does the work
    path = Path(args.source)
    if path.is_file():
        if path.suffix.lower() not in VIDEO_EXTS:
            print(f"unsupported file type {path.suffix}", file=sys.stderr)
            return 2
        src, is_new = worker.add_file(path, args.type)   # read in place: your file is never moved or deleted
    else:
        try:
            src, is_new = worker.add_url(args.source, args.type)
        except InvalidURLError as exc:
            print(f"not a video file or a usable link: {exc}", file=sys.stderr)
            return 2
    print(src.id if is_new else f"{src.id} (already known: {src.status})")
    return 0


def cmd_run(args) -> int:
    from clip_pipeline.proc import lower_process_priority

    s, store, _, worker = _parts()
    if s.low_priority:
        lower_process_priority()
    worker.start()
    worker.scan_inbox()                  # first look registers the files, the second takes the settled ones
    worker.scan_inbox()
    worker.kick()
    worker.wait_idle()
    worker.finish_background()           # cuts and renders recovered at start run outside the source queue
    failed = store.list_sources(status="failed")
    return cmd_status(args) or (1 if failed else 0)


def cmd_status(args) -> int:
    _, store, queue, _ = _parts()
    for src in reversed(store.list_sources(limit=20)):
        clips = store.list_clips(source_id=src.id)
        counts: dict[str, int] = {}
        for c in clips:
            counts[c.status] = counts.get(c.status, 0) + 1
        print(f"{src.id[:8]}  {src.status:<7} {src.stage:<8} {src.content_type or '-':<9} "
              f"{(src.title or src.url or src.file_path or '')[:48]:<48} {counts or ''}")
        if src.error:
            print(f"          error: {src.error.splitlines()[0][:150]}")
        if src.scoring_note:
            print(f"          note: {src.scoring_note[:150]}")
        for c in clips:
            if c.status != "below_threshold" or getattr(args, "all", False):
                print(f"          {c.virality_score:3d}  {c.status:<15} {c.candidate_id}  {c.title[:60]}")
    posts = queue.list_posts(limit=20)
    if posts:
        print("posts:")
        for p in posts:
            targets = ", ".join(f"{t['platform']}={t['status']}" for t in p["targets"])
            print(f"  {p['scheduled_at']}  {p['status']:<10} {p['title'][:50]:<50} {targets}")
    return 0


def cmd_pair(args) -> int:
    from clip_pipeline.auth import PAIRING_CODE_TTL, Devices, pairing_uri

    s, store, _, _ = _parts()
    code = Devices(store.db).create_pairing_code()
    uri = pairing_uri(s, code)
    try:
        import segno

        segno.make(uri, error="m").terminal(compact=True)
    except ImportError:
        print("(install the optional 'segno' package to see this as a QR code)")
    print(f"\nserver: {s.public_url}\ncode:   {code}\nlink:   {uri}")
    print(f"\nValid for {int(PAIRING_CODE_TTL.total_seconds() // 60)} minutes, for one phone.")
    if s.bind_host in ("127.0.0.1", "localhost") and not s.public_url_setting:
        print("Note: the service only listens on this machine (BIND_HOST=127.0.0.1). A phone can reach it only\n"
              "after BIND_HOST / PUBLIC_URL are set to an address the phone can open.")
    return 0


def cmd_devices(args) -> int:
    from clip_pipeline.auth import Devices

    _, store, _, _ = _parts()
    devices = Devices(store.db)
    if args.action == "revoke":
        matches = [d for d in devices.list() if d["id"].startswith(args.device_id or "\x00")]
        if len(matches) != 1:
            print("give the id (or its first characters) of exactly one device", file=sys.stderr)
            return 2
        print("revoked" if devices.revoke(matches[0]["id"]) else "was already revoked", matches[0]["name"])
    for d in devices.list():
        state = f"revoked {d['revoked_at']}" if d["revoked_at"] else "active"
        print(f"{d['id'][:8]}  {d['name']:<24} paired {d['created_at']}  last seen {d['last_seen_at'] or 'never':<20}  {state}")
    if not devices.list():
        print("no paired devices; run: python -m clip_pipeline pair")
    return 0


def _youtube_login(accounts, wanted: str) -> int:
    """Log a YouTube account in again in the browser and save the new login. This is how an account
    that could only upload gets permission to read its view counts too. Prints no secrets."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    from clip_pipeline.publish.scripts.youtube_auth import SCOPES

    matches = [a for a in accounts.list("youtube") if a["id"].startswith(wanted) or a["name"] == wanted or not wanted]
    if len(matches) != 1:
        print(f"{len(matches)} YouTube accounts match {wanted!r}; give one account's id or name")
        return 1
    acct = matches[0]
    creds = acct["credentials"]
    if not (creds.get("client_id") and creds.get("client_secret")):
        print("this account has no Google client id and secret saved, so there is nothing to log in with")
        return 1
    config = {"installed": {"client_id": creds["client_id"], "client_secret": creds["client_secret"],
                            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                            "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}
    flow = InstalledAppFlow.from_client_config(config, SCOPES)
    print("A browser window opens: choose the channel's Google account and allow both permissions.")
    granted = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not granted.refresh_token:
        print("Google returned no long-lived login; nothing was changed")
        return 1
    missing = [s for s in SCOPES if s not in (granted.scopes or SCOPES)]
    if missing:
        print("not saved: these permissions were not allowed:", ", ".join(s.rsplit("/", 1)[-1] for s in missing))
        return 1
    accounts.update(acct["id"], credentials={"refresh_token": granted.refresh_token})
    print(f"saved the new login for YouTube account {acct['name']!r}; restart the service to use it")
    return 0


def cmd_accounts(args) -> int:
    from dataclasses import replace

    from clip_pipeline.config import Settings
    from clip_pipeline.publish.accounts import Accounts

    s, store, _, _ = _parts()
    if args.action == "import":
        if args.env_file:
            # Read another env file (e.g. a backup of an old deployment) without touching this process's settings.
            from dotenv import dotenv_values

            values = {k: (v or "").strip() for k, v in dotenv_values(args.env_file).items()}
            fields = {"youtube_client_id": "YOUTUBE_CLIENT_ID", "youtube_client_secret": "YOUTUBE_CLIENT_SECRET",
                      "youtube_refresh_token": "YOUTUBE_REFRESH_TOKEN", "instagram_user_id": "INSTAGRAM_USER_ID",
                      "instagram_access_token": "INSTAGRAM_ACCESS_TOKEN"}
            s = replace(s, **{f: values[k] for f, k in fields.items() if values.get(k)})
        made = Accounts(store.db, s).import_env()
        for a in made:
            print(f"created {a['platform']} account {a['name']!r}")
        if not made:
            print("nothing to import: no complete login found, or those platforms already have an account")
    accounts = Accounts(store.db, s)
    if args.action == "login":
        return _youtube_login(accounts, args.env_file or "")
    for a in accounts.list():
        pub = accounts.public(a)
        print(f"{a['id'][:8]}  {a['platform']:<9} {a['name']:<16} {'on ' if a['enabled'] else 'off'} "
              f"login={'ok' if pub['configured'] else 'missing':<8} slots={','.join(a['slots'])}"
              + (f"  note: {a['stats_note']}" if a["stats_note"] else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="clip_pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the service")
    serve.add_argument("--host", help="address to listen on (default: BIND_HOST, i.e. this machine only)")
    serve.add_argument("--port", type=int)
    serve.set_defaults(fn=cmd_serve)
    add = sub.add_parser("add", help="queue a link or a local video file")
    add.add_argument("source")
    add.add_argument("--type", help="content type, if you already know it (film, podcast, gaming, …)")
    add.set_defaults(fn=cmd_add)
    run = sub.add_parser("run", help="process the queue in this process, then exit")
    run.add_argument("--all", action="store_true", help="also list clips below the review score")
    run.set_defaults(fn=cmd_run)
    status = sub.add_parser("status", help="show sources, clips and posts")
    status.add_argument("--all", action="store_true", help="also list clips below the review score")
    status.set_defaults(fn=cmd_status)
    sub.add_parser("pair", help="show a one-time code to pair a phone").set_defaults(fn=cmd_pair)
    dev = sub.add_parser("devices", help="list paired phones, or revoke one")
    dev.add_argument("action", nargs="?", choices=["list", "revoke"], default="list")
    dev.add_argument("device_id", nargs="?")
    dev.set_defaults(fn=cmd_devices)
    acc = sub.add_parser("accounts", help="list accounts, import logins from an env file, or log YouTube in again")
    acc.add_argument("action", nargs="?", choices=["list", "import", "login"], default="list")
    acc.add_argument("env_file", nargs="?", metavar="env_file|account",
                     help="import: env file to read logins from (default: this project's settings); "
                          "login: id or name of the YouTube account")
    acc.set_defaults(fn=cmd_accounts)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
