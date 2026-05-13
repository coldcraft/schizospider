from __future__ import annotations

import asyncio
import time
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    RichLog,
    Static,
)

from schizospider.config import Settings
from schizospider.crawler import Crawler
from schizospider.events import Bus, Event
from schizospider.report.build import build_report
from schizospider.store import DONE, ERROR, IN_FLIGHT, PageRow, QUEUED, Store

STATE_GLYPHS = {QUEUED: "·", IN_FLIGHT: "▶", DONE: "✓", ERROR: "✗", "skipped": "↷"}


class CounterBar(Static):
    queued = reactive(0)
    in_flight = reactive(0)
    done = reactive(0)
    errors = reactive(0)
    elapsed = reactive(0)
    seed = reactive("")
    paused = reactive(False)
    p50_ms = reactive(0)
    p95_ms = reactive(0)

    def render(self) -> str:
        mm, ss = divmod(int(self.elapsed), 60)
        hh, mm = divmod(mm, 60)
        timing = ""
        if self.p50_ms > 0:
            timing = f"  p50:{self.p50_ms}ms p95:{self.p95_ms}ms"
        return (
            f"[b]schizospider[/b]  seed=[cyan]{self.seed}[/cyan]  "
            f"Q:[yellow]{self.queued}[/yellow] "
            f"▶:[blue]{self.in_flight}[/blue] "
            f"✓:[green]{self.done}[/green] "
            f"✗:[red]{self.errors}[/red]  "
            f"⏱ {hh:02d}:{mm:02d}:{ss:02d}"
            f"{timing}"
            + ("  [bold red][PAUSED][/bold red]" if self.paused else "")
        )


class WorkerBar(Static):
    """Shows what each worker is currently fetching, with elapsed time."""

    def __init__(self) -> None:
        super().__init__("", id="workerbar")
        self.lines: list[str] = []

    def update_workers(self, snap: list[tuple[int, str, float]]) -> None:
        if not snap:
            self.update("[dim](no active workers)[/]")
            return
        rows = []
        for wid, url, secs in sorted(snap):
            shown = url
            if len(shown) > 80:
                shown = shown[:40] + "…" + shown[-39:]
            rows.append(f"  w{wid}  [{secs:5.1f}s]  {shown}")
        self.update("\n".join(rows))


class DetailPanel(Static):
    def __init__(self) -> None:
        super().__init__("Select a URL on the left to see details here.", id="detail")
        self.current_page_id: Optional[int] = None

    async def show_page(self, store: Store, page_id: int) -> None:
        self.current_page_id = page_id
        page = await store.get_page(page_id)
        if not page:
            self.update("(page not found)")
            return
        ob = await store.outbound_links(page_id)
        ib = await store.inbound_links(page_id)

        title = page.title or "(untitled)"
        lines = [
            f"[b]{title}[/b]",
            f"[cyan]{page.url_canonical}[/cyan]",
            "",
            f"state:  [green]{page.state}[/green]   "
            f"status: {page.http_status or '—'}   depth: {page.depth}",
            f"domain: {page.registrable_domain} "
            f"{'(on-domain)' if page.is_seed_domain else '(off-domain)'}",
            f"content-type: {page.content_type or '—'}",
            f"screenshot: {page.screenshot_path or '—'}",
            f"html:       {page.html_path or '—'}",
        ]
        if page.error:
            lines.append(f"[red]error:[/red] {page.error}")
        lines.append("")
        lines.append(f"[b]outbound ({len(ob)})[/b]")
        for dst_id, kind, anchor in ob[:30]:
            dst = await store.get_page(dst_id)
            if not dst:
                continue
            badge = "on" if dst.is_seed_domain else "off"
            lines.append(f"  [{kind}] [{badge}] {dst.url_canonical}")
        if len(ob) > 30:
            lines.append(f"  … and {len(ob) - 30} more")
        lines.append("")
        lines.append(f"[b]inbound ({len(ib)})[/b]")
        for src_id, anchor in ib[:30]:
            src = await store.get_page(src_id)
            if not src:
                continue
            lines.append(f"  ← {src.url_canonical}")
        if len(ib) > 30:
            lines.append(f"  … and {len(ib) - 30} more")
        self.update("\n".join(lines))


class Schizospider(App):
    TITLE = "schizospider"
    SUB_TITLE = ""
    CSS = """
    Screen { layout: vertical; }
    CounterBar { height: 1; padding: 0 1; background: $panel; }
    WorkerBar { height: auto; max-height: 6; padding: 0 1; background: $panel; color: $text-muted; }
    #body { height: 1fr; }
    #urllist { width: 45%; }
    #detail { width: 1fr; padding: 1; }
    RichLog { height: 8; background: $panel; }
    Input { dock: bottom; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "build_report", "report"),
        Binding("p", "toggle_pause", "pause/resume"),
        Binding("/", "focus_filter", "filter"),
        Binding("escape", "blur_filter", show=False),
    ]

    def __init__(self, settings: Settings, store: Store, bus: Bus, crawler: Crawler):
        super().__init__()
        self.settings = settings
        self.store = store
        self.bus = bus
        self.crawler = crawler
        self._start = time.time()
        self._row_id_to_key: dict[int, str] = {}
        self._filter_text: str = ""
        # Seed URL shows up in the Textual header next to the title.
        self.sub_title = settings.seed

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        self.counter = CounterBar(id="counterbar")
        self.counter.seed = self.settings.seed
        yield self.counter
        self.workerbar = WorkerBar()
        yield self.workerbar
        with Horizontal(id="body"):
            self.urllist = DataTable(id="urllist", cursor_type="row", zebra_stripes=True)
            self.urllist.add_columns("st", "url", "status", "d", "out")
            yield self.urllist
            self.detail = DetailPanel()
            yield self.detail
        self.log_widget = RichLog(highlight=True, markup=True, id="log")
        yield self.log_widget
        self.filter_input = Input(placeholder="/filter (esc to cancel)", id="filter")
        self.filter_input.display = False
        yield self.filter_input
        yield Footer()

    def on_mount(self) -> None:
        # Run crawler concurrently.
        self.crawler_task = asyncio.create_task(self._crawl_wrapper())
        # Periodic counter refresh.
        self.set_interval(0.5, self._refresh_counters)
        # Event consumer.
        self._event_queue = self.bus.subscribe()
        self._event_task = asyncio.create_task(self._consume_events())
        # Initial population (in case of resume).
        asyncio.create_task(self._initial_populate())

    async def _crawl_wrapper(self) -> None:
        try:
            await self.crawler.run()
            self._safe_log("[bold green]crawl complete[/]")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._safe_log(f"[red]crawler crashed: {e}[/]")

    def _safe_log(self, msg: str) -> None:
        """Write to log widget unless the app is being torn down."""
        try:
            self.log_widget.write(msg)
        except Exception:
            pass

    async def _consume_events(self) -> None:
        try:
            while True:
                event: Event = await self._event_queue.get()
                if event.kind in ("page_enqueued", "page_done", "page_failed", "page_updated"):
                    await self._refresh_row(event.payload)
                elif event.kind == "log":
                    self._safe_log(str(event.payload))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _initial_populate(self) -> None:
        # Wait a tick so store is open.
        await asyncio.sleep(0.05)
        pages = await self.store.list_pages(limit=10000)
        for p in pages:
            self._upsert_row(p)

    async def _refresh_row(self, page_id: int) -> None:
        p = await self.store.get_page(page_id)
        if p is None:
            return
        self._upsert_row(p)
        if self.detail.current_page_id == page_id:
            await self.detail.show_page(self.store, page_id)

    def _upsert_row(self, p: PageRow) -> None:
        if not self._matches_filter(p):
            return
        glyph = STATE_GLYPHS.get(p.state, "?")
        url_disp = p.url_canonical
        if len(url_disp) > 60:
            url_disp = url_disp[:30] + "…" + url_disp[-29:]
        out_count_str = "—"
        status_str = "—" if p.http_status is None else str(p.http_status)
        depth_str = str(p.depth)
        row_data = (glyph, url_disp, status_str, depth_str, out_count_str)
        key = self._row_id_to_key.get(p.id)
        if key is None:
            key = self.urllist.add_row(*row_data, key=str(p.id))
            self._row_id_to_key[p.id] = str(p.id)
        else:
            try:
                self.urllist.update_cell(key, "st", glyph)
                self.urllist.update_cell(key, "url", url_disp)
                self.urllist.update_cell(key, "status", status_str)
                self.urllist.update_cell(key, "d", depth_str)
            except Exception:
                # Cells are sometimes keyed by column index; fall back.
                pass

    def _matches_filter(self, p: PageRow) -> bool:
        if not self._filter_text:
            return True
        t = self._filter_text.lower()
        hay = (p.url_canonical + " " + (p.title or "")).lower()
        return t in hay

    async def _refresh_counters(self) -> None:
        self.counter.queued = await self.store.queue_size()
        self.counter.in_flight = await self.store.in_flight_size()
        self.counter.done = await self.store.done_count()
        self.counter.errors = await self.store.error_count()
        self.counter.elapsed = time.time() - self._start
        self.counter.paused = self.crawler.is_paused
        p50 = self.crawler.fetch_percentile_ms(0.50)
        p95 = self.crawler.fetch_percentile_ms(0.95)
        self.counter.p50_ms = p50 or 0
        self.counter.p95_ms = p95 or 0
        try:
            self.workerbar.update_workers(self.crawler.active_workers_view())
        except Exception:
            pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value if hasattr(event.row_key, "value") else event.row_key
        try:
            page_id = int(str(key))
        except (TypeError, ValueError):
            return
        asyncio.create_task(self.detail.show_page(self.store, page_id))

    # Actions

    async def action_quit(self) -> None:
        self.crawler.stop()
        # Try to let the crawler unwind cleanly, but don't wait forever.
        # Stuck Playwright RPCs can ignore stopping; cancel after a short grace.
        task = getattr(self, "crawler_task", None)
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                task.cancel()
        self.exit()

    async def action_build_report(self) -> None:
        self.log_widget.write("[yellow]building report…[/]")
        try:
            path = await build_report(self.settings, self.store)
            self.log_widget.write(f"[green]report: {path}[/]")
        except Exception as e:
            self.log_widget.write(f"[red]report failed: {e}[/]")

    def action_toggle_pause(self) -> None:
        if self.crawler.is_paused:
            self.crawler.resume()
            self.log_widget.write("[yellow]resumed[/]")
        else:
            self.crawler.pause()
            self.log_widget.write("[yellow]paused[/]")

    def action_focus_filter(self) -> None:
        self.filter_input.display = True
        self.filter_input.focus()

    def action_blur_filter(self) -> None:
        self.filter_input.display = False
        self.urllist.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._filter_text = event.value.strip()
        self.filter_input.display = False
        # Rebuild table.
        self.urllist.clear()
        self._row_id_to_key.clear()
        asyncio.create_task(self._initial_populate())
        self.urllist.focus()
