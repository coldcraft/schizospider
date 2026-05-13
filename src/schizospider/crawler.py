from __future__ import annotations

import asyncio
import bisect
import logging
import time
from collections import defaultdict, deque
from typing import Optional

from schizospider.config import Settings
from schizospider.events import Bus
from schizospider.fetcher import fetch, launch_browser, new_worker_context
from schizospider.store import (
    DONE,
    ERROR,
    PageRow,
    Store,
    sha16,
)
from schizospider.urls import host_of, same_site

log = logging.getLogger("schizospider.crawler")

# Hard ceiling per page so a single pathological URL can never stall a worker
# indefinitely. Per-step timeouts inside fetcher add up to ~70s worst case;
# this outer guard catches stuck Playwright RPC calls that ignore their own
# timeouts (e.g. evaluate() blocked on a hostile JS loop).
PAGE_HARD_TIMEOUT_S = 90


class Crawler:
    def __init__(self, settings: Settings, store: Store, bus: Bus):
        self.settings = settings
        self.store = store
        self.bus = bus
        self.stopping = asyncio.Event()
        self.paused = asyncio.Event()
        self.paused.set()  # initially "not paused" — set means proceed
        # Per-host last-fetch timestamps (sec). Enforces politeness without
        # holding a lock that blocks other workers on the same host.
        self._last_fetch_at: dict[str, float] = defaultdict(float)
        self._host_pace_lock = asyncio.Lock()
        self._idle_workers = 0
        self._worker_count = 0
        # Rolling window of recent fetch times (ms) for p50/p95 in the TUI.
        self._fetch_times_ms: deque[int] = deque(maxlen=200)
        # Worker -> "url currently being fetched" so the TUI can show progress.
        self._worker_urls: dict[int, str] = {}
        self._worker_started_at: dict[int, float] = {}

    # --- stats (for TUI) ---
    def fetch_percentile_ms(self, pct: float) -> Optional[int]:
        """Return p50/p95/etc of fetch durations (ms). None if not enough samples."""
        if len(self._fetch_times_ms) < 5:
            return None
        sorted_times = sorted(self._fetch_times_ms)
        idx = max(0, min(len(sorted_times) - 1, int(round(pct * (len(sorted_times) - 1)))))
        return sorted_times[idx]

    def active_workers_view(self) -> list[tuple[int, str, float]]:
        """Snapshot of (worker_id, url, seconds_elapsed) for active workers."""
        now = time.time()
        out = []
        for wid, url in list(self._worker_urls.items()):
            started = self._worker_started_at.get(wid, now)
            out.append((wid, url, now - started))
        return out

    # --- control ---
    def stop(self) -> None:
        self.stopping.set()
        self.paused.set()  # unblock any waiting workers so they can exit

    def pause(self) -> None:
        self.paused.clear()

    def resume(self) -> None:
        self.paused.set()

    @property
    def is_paused(self) -> bool:
        return not self.paused.is_set()

    # --- main loop ---
    async def run(self) -> None:
        await self.store.enqueue_seed()
        async with launch_browser(self.settings) as (_, browser):
            contexts = []
            try:
                for _ in range(self.settings.concurrency):
                    contexts.append(await new_worker_context(browser, self.settings))
                self._worker_count = len(contexts)
                workers = [
                    asyncio.create_task(self._worker(i, ctx))
                    for i, ctx in enumerate(contexts)
                ]
                await asyncio.gather(*workers, return_exceptions=True)
            finally:
                for ctx in contexts:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

    async def _worker(self, worker_id: int, ctx) -> None:
        log.debug("worker %d start", worker_id)
        while not self.stopping.is_set():
            await self.paused.wait()
            if self.stopping.is_set():
                break

            # Honor max-pages cap (counts done + error pages).
            if self.settings.max_pages > 0:
                done = await self.store.done_count()
                err = await self.store.error_count()
                if done + err >= self.settings.max_pages:
                    self.stop()
                    break

            row = await self.store.lease_next()
            if row is None:
                # No work right now. If everyone else is also idle AND queue
                # and in-flight are both empty, we're done — signal all workers
                # to exit, otherwise the worker that *doesn't* hit the predicate
                # itself will loop forever.
                self._idle_workers += 1
                try:
                    qsz = await self.store.queue_size()
                    ifsz = await self.store.in_flight_size()
                    if qsz == 0 and ifsz == 0 and self._idle_workers >= self._worker_count:
                        self.stopping.set()
                        break
                    await asyncio.sleep(0.25)
                    continue
                finally:
                    self._idle_workers -= 1

            await self._process(worker_id, ctx, row)
        log.debug("worker %d done", worker_id)

    async def _process(self, worker_id: int, ctx, row: PageRow) -> None:
        host = host_of(row.url_canonical)
        await self._wait_for_host_pace(host)
        start = time.time()
        self._worker_urls[worker_id] = row.url_canonical
        self._worker_started_at[worker_id] = start
        try:
            try:
                result = await asyncio.wait_for(
                    fetch(ctx, row.url_canonical, self.settings),
                    timeout=PAGE_HARD_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                await self.store.fail(
                    row.id, f"timeout: page exceeded {PAGE_HARD_TIMEOUT_S}s hard ceiling"
                )
                self.bus.publish(
                    "log", f"TMO {row.url_canonical} ({PAGE_HARD_TIMEOUT_S}s ceiling)"
                )
                return
            except Exception as e:
                await self.store.fail(row.id, f"unexpected: {type(e).__name__}: {e}")
                self.bus.publish("log", f"ERR {row.url_canonical}: {e}")
                return

            elapsed_ms = int((time.time() - start) * 1000)
            self._fetch_times_ms.append(elapsed_ms)

            if result.error:
                await self.store.fail(row.id, result.error)
                self.bus.publish(
                    "log", f"ERR {row.url_canonical} ({elapsed_ms}ms): {result.error}"
                )
                return

            screenshot_rel = await self._write_screenshot(row, result)
            html_rel = await self._write_html(row, result)

            await self.store.complete(
                row.id, result, screenshot_rel=screenshot_rel, html_rel=html_rel
            )

            on_domain = same_site(
                result.final_url or row.url_canonical,
                self.settings.seed,
                self.settings.host_mode,
            )
            if on_domain:
                await self.store.enqueue_many(
                    result.outlinks, depth=row.depth + 1, src_id=row.id
                )
            else:
                await self.store.record_links_only(row.id, result.outlinks)

            self.bus.publish(
                "log",
                f"GOT {row.url_canonical} -> {result.http_status} "
                f"({elapsed_ms}ms, {len(result.outlinks)} links)",
            )
        finally:
            self._worker_urls.pop(worker_id, None)
            self._worker_started_at.pop(worker_id, None)

    async def _wait_for_host_pace(self, host: str) -> None:
        """Enforce minimum gap between consecutive starts for the same host.

        Uses a brief shared lock just to read/update the timestamp map —
        we do NOT hold a lock during the actual fetch, so other workers
        can fetch other hosts (or the same host once the gap elapses).
        """
        if self.settings.politeness_ms <= 0:
            return
        gap = self.settings.politeness_ms / 1000.0
        async with self._host_pace_lock:
            now = time.time()
            earliest = self._last_fetch_at[host] + gap
            wait = max(0.0, earliest - now)
            self._last_fetch_at[host] = max(now, earliest)
        if wait > 0:
            await asyncio.sleep(wait)

    async def _write_screenshot(self, row: PageRow, result) -> Optional[str]:
        if not result.screenshot_bytes:
            return None
        try:
            name = f"{sha16(row.url_canonical)}.png"
            path = self.settings.screenshots_dir / name
            await asyncio.to_thread(path.write_bytes, result.screenshot_bytes)
            return f"screenshots/{name}"
        except Exception as e:
            log.debug("screenshot write failed: %s", e)
            return None

    async def _write_html(self, row: PageRow, result) -> Optional[str]:
        if not result.html:
            return None
        try:
            name = f"{sha16(row.url_canonical)}.html"
            path = self.settings.pages_dir / name
            await asyncio.to_thread(
                path.write_text, result.html, "utf-8", "replace"
            )
            return f"pages/{name}"
        except Exception as e:
            log.debug("html write failed: %s", e)
            return None
