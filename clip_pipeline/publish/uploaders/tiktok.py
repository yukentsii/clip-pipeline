"""TikTok via Playwright, driving TikTok Studio with a persistent, already-logged-in profile.

One-time setup on any machine with a screen (the server itself can stay headless):
    python -m playwright install chromium
    python -m clip_pipeline.publish.uploaders.tiktok --login --state tiktok_state.json   # log in by hand
then copy tiktok_state.json to the server's TIKTOK_STORAGE_STATE path (chmod 600 - it holds
session cookies). The uploader refreshes that file after every successful post.

If TikTok shows a login wall or CAPTCHA the job fails as non-retryable so a
human can re-run --login; the worker never tries to solve challenges itself.
Web automation is brittle and may conflict with TikTok's terms; TikTok's
official Content Posting API is the sanctioned alternative once your app is approved.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

from clip_pipeline.config import get_settings
from clip_pipeline.publish.uploaders.base import BaseUploader, UploadError, UploadJob, UploadResult, build_caption

log = logging.getLogger(__name__)

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=upload"
LOGIN_URL = "https://www.tiktok.com/login"

# Selectors live in one place because TikTok changes its DOM regularly.
SEL_FILE_INPUT = 'input[type="file"]'
SEL_CAPTION = 'div[contenteditable="true"]'
SEL_POST_BUTTON = 'button[data-e2e="post_video_button"]'
SEL_UPLOADED = 'text=/Uploaded|Upload complete/i'
SEL_CAPTCHA = '[id*="captcha" i], [class*="captcha" i]'
SEL_POSTED = 'text=/Your video has been (uploaded|posted)|Manage your posts|Video published/i'

UPLOAD_TIMEOUT_MS = 10 * 60 * 1000
VIEWPORT = {"width": 1366, "height": 900}
DISMISS_LABELS = ("Cancel", "Not now", "Skip", "Later", "Got it", "Close", "OK")
# Modal dialogs plus react-joyride onboarding tour cards ("New editing features added → Got it").
BLOCKING_POPUPS = ("[role=dialog]:visible", "[role=alertdialog]:visible", ".react-joyride__tooltip:visible")
JOYRIDE_OVERLAY = ".react-joyride__overlay:visible"
SESSION_COOKIES = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt"}


class TikTokUploader(BaseUploader):
    platform = "tiktok"

    def __init__(self, settings=None):
        self.s = settings or get_settings()

    def is_configured(self) -> bool:
        if self.s.tiktok_storage_state.is_file():
            return True
        d = self.s.tiktok_user_data_dir
        return d.exists() and any(d.iterdir())

    def _open_context(self, p):
        """Prefer a portable storage-state file (login done on another machine);
        fall back to a persistent profile directory logged in on this machine."""
        if self.s.tiktok_storage_state.is_file():
            browser = p.chromium.launch(headless=self.s.tiktok_headless)
            ctx = browser.new_context(storage_state=str(self.s.tiktok_storage_state), viewport=VIEWPORT)
            return ctx, browser
        ctx = p.chromium.launch_persistent_context(str(self.s.tiktok_user_data_dir),
                                                   headless=self.s.tiktok_headless, viewport=VIEWPORT)
        return ctx, None

    def upload(self, job: UploadJob) -> UploadResult:
        self._drive(job, publish=True)
        return UploadResult(remote_id=f"tiktok-{int(time.time())}")

    def preflight(self, job: UploadJob, screenshot: Path | None = None) -> str:
        """Everything except clicking Post: session, file upload, caption typing, Post button enabled."""
        self._drive(job, publish=False, screenshot=screenshot)
        return "session valid, video uploaded to Studio, caption typed, Post button enabled (not clicked)"

    def _drive(self, job: UploadJob, *, publish: bool, screenshot: Path | None = None) -> None:
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright

        caption = build_caption(job.title, "", job.tags, max_len=2000)
        try:
            with sync_playwright() as p:
                ctx, browser = self._open_context(p)
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60_000)
                    self._guard(page)

                    page.locator(SEL_FILE_INPUT).first.set_input_files(str(job.file_path), timeout=60_000)
                    page.locator(SEL_UPLOADED).first.wait_for(timeout=UPLOAD_TIMEOUT_MS)
                    self._guard(page)
                    page.wait_for_timeout(1500)
                    self._dismiss_popups(page)

                    editor = page.locator(SEL_CAPTION).first
                    editor.click()
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    for i, line in enumerate(caption.split("\n")):
                        if i:
                            page.keyboard.press("Shift+Enter")
                        for word in line.split(" "):
                            page.keyboard.type(word, delay=15)
                            # a trailing space closes TikTok's hashtag/mention suggestion popover
                            page.keyboard.type(" ", delay=15)
                            if word.startswith("#"):
                                time.sleep(0.6)

                    self._dismiss_popups(page)
                    post = page.locator(SEL_POST_BUTTON).first
                    post.wait_for(state="visible", timeout=60_000)
                    deadline = time.monotonic() + 120
                    while post.is_disabled() and time.monotonic() < deadline:
                        time.sleep(1)
                    if screenshot is not None:
                        page.screenshot(path=str(screenshot), full_page=False)
                    if not publish:
                        if post.is_disabled():
                            raise UploadError("Post button never became enabled", retryable=True)
                        return
                    post.click()
                    page.locator(SEL_POSTED).first.wait_for(timeout=180_000)
                    if browser is not None:  # keep rotated session cookies for next time
                        save_state(ctx, self.s.tiktok_storage_state)
                finally:
                    ctx.close()
                    if browser is not None:
                        browser.close()
        except UploadError:
            raise
        except PWTimeout as exc:
            raise UploadError(f"TikTok UI timed out: {exc}", retryable=True) from exc
        except PWError as exc:
            raise UploadError(f"TikTok automation error: {exc}", retryable=True) from exc

    @staticmethod
    def _dismiss_popups(page, rounds: int = 6) -> list[str]:
        """Close one-off Studio dialogs (e.g. "Turn on automatic content checks?") that block clicks.
        Always picks the option that leaves account settings unchanged (Cancel / Not now / close)."""
        closed = []
        for _ in range(rounds):
            # topmost first: a modal dialog sits above tour cards and swallows their clicks
            dialog = next((page.locator(sel).first for sel in BLOCKING_POPUPS if page.locator(sel).count()), None)
            if dialog is None:
                if page.locator(JOYRIDE_OVERLAY).count():  # tour overlay with no tooltip left: click it away
                    page.locator(JOYRIDE_OVERLAY).first.click(force=True)
                    page.wait_for_timeout(500)
                    continue
                break
            title = (dialog.inner_text() or "").split("\n", 1)[0][:80]
            for label in DISMISS_LABELS:
                btn = dialog.get_by_role("button", name=label, exact=True)
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    break
            else:
                page.keyboard.press("Escape")
            closed.append(title)
            page.wait_for_timeout(800)
        if closed:
            log.info("Dismissed TikTok dialogs: %s", closed)
        return closed

    @staticmethod
    def _guard(page) -> None:
        if "/login" in page.url:
            raise UploadError("TikTok session expired: run `python -m clip_pipeline.publish.uploaders.tiktok --login`",
                              retryable=False)
        if page.locator(SEL_CAPTCHA).count() and page.locator(SEL_CAPTCHA).first.is_visible():
            raise UploadError("TikTok is showing a CAPTCHA: complete it manually with --login", retryable=False)


def save_state(ctx, path: Path) -> None:
    """Write Playwright storage state (cookies + localStorage) owner-readable only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    ctx.storage_state(path=str(tmp))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def interactive_login(state_path: Path | None = None, timeout_s: int = 900) -> None:
    """Open a headed browser, wait until the user has logged in, then save a portable
    storage-state file. Run it on any machine with a screen and copy the file to the
    server's TIKTOK_STORAGE_STATE path (it contains session cookies: treat as a secret)."""
    from playwright.sync_api import sync_playwright

    state_path = state_path or get_settings().tiktok_storage_state
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(viewport=VIEWPORT)
        page = ctx.new_page()
        page.goto(LOGIN_URL)
        print("Log in to TikTok in the opened window; it is detected automatically.", flush=True)
        def logged_in() -> bool:
            names = {c["name"] for c in ctx.cookies() if c["value"] and "tiktok.com" in c["domain"]}
            return bool(names & SESSION_COOKIES)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if logged_in():
                break
            if page.is_closed():  # user closed the tab: the context (and its cookies) survive
                if logged_in():
                    break
                raise SystemExit("Window closed before login completed")
            time.sleep(2)
        else:
            raise SystemExit("Timed out waiting for TikTok login")
        time.sleep(3)  # let TikTok finish setting the remaining session cookies
        if not page.is_closed():
            page.goto(UPLOAD_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
        save_state(ctx, state_path)
        print(f"Logged in; session saved ({state_path.name}).", flush=True)
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TikTok uploader utilities")
    parser.add_argument("--login", action="store_true", help="open a headed browser to log in once")
    parser.add_argument("--state", type=Path, help="where to write the storage-state file")
    args = parser.parse_args()
    if args.login:
        interactive_login(args.state)
    else:
        parser.print_help()
