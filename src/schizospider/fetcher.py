from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from schizospider.config import Settings
from schizospider.extractor import extract_from_page
from schizospider.store import FetchResult

log = logging.getLogger("schizospider.fetcher")


@asynccontextmanager
async def launch_browser(settings: Settings):
    pw: Playwright = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(
            headless=settings.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        try:
            yield pw, browser
        finally:
            await browser.close()
    finally:
        await pw.stop()


async def new_worker_context(
    browser: Browser, settings: Settings
) -> BrowserContext:
    ctx = await browser.new_context(
        ignore_https_errors=True,
        user_agent=settings.user_agent,
        viewport={"width": 1366, "height": 900},
        java_script_enabled=True,
    )
    ctx.set_default_navigation_timeout(settings.nav_timeout_ms)
    ctx.set_default_timeout(settings.nav_timeout_ms)

    # Dismiss any dialog (alert/confirm/prompt) — jodi.org loves these.
    ctx.on("dialog", lambda d: asyncio.create_task(_safe_dismiss(d)))

    if settings.block_media:
        await ctx.route(
            "**/*.{mp4,webm,mov,avi,m4v,mkv,mp3,wav,ogg}",
            lambda route: asyncio.create_task(_safe_abort(route)),
        )

    return ctx


async def _safe_dismiss(dialog) -> None:
    try:
        await dialog.dismiss()
    except Exception:
        pass


async def _safe_close(page) -> None:
    try:
        await page.close()
    except Exception:
        pass


async def _safe_abort(route) -> None:
    try:
        await route.abort()
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass


async def fetch(
    ctx: BrowserContext,
    url: str,
    settings: Settings,
) -> FetchResult:
    """Navigate to `url`, take a full-page screenshot, harvest outlinks."""
    page: Page = await ctx.new_page()
    # Close any popup window.open() spawns — but only popups from *this* page.
    page.on("popup", lambda p: asyncio.create_task(_safe_close(p)))

    try:
        response = None
        nav_warning: Optional[str] = None
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=settings.nav_timeout_ms,
            )
        except PlaywrightTimeoutError:
            nav_warning = "navigation: domcontentloaded timeout"
        except Exception as e:
            # ERR_ABORTED, ERR_FAILED, etc. — record but try to continue;
            # the page may still have content (jodi.org's framesets trigger this).
            nav_warning = f"navigation: {type(e).__name__}: {e}"

        # Best-effort: give frames a moment to settle, but don't block forever.
        try:
            await page.wait_for_load_state("networkidle", timeout=4_000)
        except Exception:
            pass

        final_url = page.url or url
        # If after all that the page is still about:blank, we truly couldn't get there.
        if final_url == "about:blank":
            return FetchResult(
                final_url=url,
                http_status=None,
                content_type=None,
                title=None,
                headers={},
                html=None,
                screenshot_bytes=None,
                outlinks=[],
                error=nav_warning or "navigation: page never loaded",
            )
        http_status: Optional[int] = None
        headers: dict[str, str] = {}
        content_type: Optional[str] = None
        if response is not None:
            try:
                http_status = response.status
            except Exception:
                pass
            try:
                headers = dict(response.headers)
                content_type = headers.get("content-type")
            except Exception:
                pass

        # Title + outlinks (multi-frame aware).
        title = ""
        outlinks: list[tuple[str, str, str]] = []
        try:
            title, outlinks = await extract_from_page(page)
        except Exception as e:
            log.debug("extract failed for %s: %s", url, e)

        # Captured HTML of top frame.
        html: Optional[str] = None
        try:
            html = await page.content()
            if html and len(html.encode("utf-8", errors="replace")) > settings.max_html_bytes:
                html = html[: settings.max_html_bytes // 2]  # cap
        except Exception:
            pass

        # Full-page screenshot. If page is huge, clip.
        screenshot_bytes: Optional[bytes] = None
        try:
            screenshot_bytes = await page.screenshot(
                full_page=True, type="png", timeout=15_000
            )
        except Exception:
            try:
                screenshot_bytes = await page.screenshot(
                    full_page=False, type="png", timeout=5_000
                )
            except Exception as e:
                log.debug("screenshot failed for %s: %s", url, e)

        return FetchResult(
            final_url=final_url,
            http_status=http_status,
            content_type=content_type,
            title=title,
            headers=headers,
            html=html,
            screenshot_bytes=screenshot_bytes,
            outlinks=outlinks,
            error=None,
        )
    finally:
        try:
            await page.close()
        except Exception:
            pass
