from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import click

from schizospider.config import Settings, default_run_id
from schizospider.crawler import Crawler
from schizospider.events import Bus
from schizospider.report.build import build_report
from schizospider.store import Store


def _make_settings(
    seed: str,
    run_id: Optional[str],
    out: str,
    concurrency: int,
    politeness_ms: int,
    host_mode: str,
    respect_robots: bool,
    block_media: bool,
    headless: bool,
    max_pages: int = 0,
    blocked_hosts: tuple[str, ...] = (),
) -> Settings:
    rid = run_id or default_run_id(seed)
    out_root = Path(out).resolve()
    kwargs = dict(
        seed=seed,
        run_id=rid,
        out_root=out_root,
        concurrency=concurrency,
        politeness_ms=politeness_ms,
        host_mode=host_mode,
        respect_robots=respect_robots,
        block_media=block_media,
        headless=headless,
        max_pages=max_pages,
    )
    if blocked_hosts:
        kwargs["blocked_hosts"] = blocked_hosts
    return Settings(**kwargs)


async def _run_crawl(settings: Settings, use_tui: bool) -> None:
    settings.ensure_dirs()
    bus = Bus()
    store = Store(
        settings.db_path,
        seed_url=settings.seed,
        host_mode=settings.host_mode,
        bus=bus,
        blocked_hosts=settings.blocked_hosts,
    )
    await store.open()
    await store.set_meta("seed", settings.seed)
    await store.set_meta("run_id", settings.run_id)

    crawler = Crawler(settings, store, bus)
    crawler_task: Optional[asyncio.Task] = None

    try:
        if use_tui:
            from schizospider.tui.app import Schizospider

            app = Schizospider(settings, store, bus, crawler)
            try:
                await app.run_async()
            except (KeyboardInterrupt, asyncio.CancelledError):
                # Ensure we always reach the cleanup block below.
                pass
            crawler_task = getattr(app, "crawler_task", None)
        else:
            # Headless console mode: subscribe and print, then build report.
            sub = bus.subscribe(maxsize=4096)
            printer = asyncio.create_task(_console_printer(sub))
            try:
                await crawler.run()
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                printer.cancel()
                try:
                    await printer
                except (asyncio.CancelledError, Exception):
                    pass
    finally:
        # Always: signal stop, drain crawler task with strict timeout,
        # then build the final report and close store.
        crawler.stop()
        if crawler_task and not crawler_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(crawler_task), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                crawler_task.cancel()
                try:
                    await asyncio.wait_for(crawler_task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
        try:
            # Outer ceiling so a wedged report build (network fs lock, Pillow
            # stuck, etc) can never trap the user in a hanging process forever.
            path = await asyncio.wait_for(build_report(settings, store), timeout=300)
            click.echo(f"report: {path}")
        except asyncio.TimeoutError:
            click.echo(
                "final report timed out (>5 min). Re-run `schizospider "
                f"--report-only {settings.run_id}` later to retry.",
                err=True,
            )
        except Exception as e:
            click.echo(f"final report failed: {e}", err=True)
        try:
            await store.close()
        except Exception:
            pass


async def _console_printer(sub) -> None:
    while True:
        ev = await sub.get()
        if ev.kind == "log":
            click.echo(str(ev.payload))


async def _rescan_html(run_id: str, out: str, strict_host: bool) -> None:
    """Re-extract links from already-captured pages/<sha>.html and enqueue new ones.

    Use case: extractor was upgraded (new pattern recognized — e.g. <form action>)
    and you want to surface URLs that were missed without re-crawling from scratch.
    """
    import sqlite3
    from schizospider.extractor import extract_from_html

    out_root = Path(out).resolve()
    run_dir = out_root / run_id
    db_path = run_dir / "db.sqlite"
    if not db_path.exists():
        click.echo(f"no db at {db_path}", err=True)
        sys.exit(2)

    # Pull seed so the store can classify on-domain vs off-domain.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    seed_row = conn.execute("SELECT value FROM meta WHERE key='seed'").fetchone()
    conn.close()
    seed = seed_row["value"] if seed_row else "about:blank"

    settings = _make_settings(
        seed=seed,
        run_id=run_id,
        out=out,
        concurrency=4,
        politeness_ms=250,
        host_mode="strict" if strict_host else "registrable",
        respect_robots=False,
        block_media=True,
        headless=True,
    )

    bus = Bus()
    store = Store(
        settings.db_path,
        seed_url=settings.seed,
        host_mode=settings.host_mode,
        bus=bus,
    )
    await store.open()
    pages = await store.list_pages()
    scanned = 0
    new_total = 0
    for p in pages:
        if not p.html_path:
            continue
        html_file = run_dir / p.html_path
        if not html_file.exists():
            continue
        try:
            raw = html_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        outlinks = extract_from_html(raw, p.url_canonical)
        if outlinks:
            new_ids = await store.enqueue_many(outlinks, depth=p.depth + 1, src_id=p.id)
            new_total += len(new_ids)
        scanned += 1
    await store.close()
    click.echo(
        f"rescanned {scanned} captured pages, enqueued {new_total} newly-discovered URLs"
    )
    click.echo(
        f"resume the crawl to fetch them: schizospider --seed {seed} --run-id {run_id}"
    )


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--seed", help="URL to start crawling from.")
@click.option("--run-id", default=None, help="Run identifier (auto if omitted).")
@click.option("--out", default="out", show_default=True, help="Output root directory.")
@click.option("--concurrency", default=4, show_default=True, type=int)
@click.option("--politeness-ms", default=250, show_default=True, type=int)
@click.option(
    "--strict-host/--include-subdomains",
    default=False,
    help="Strict host match vs registrable-domain match (default: include subdomains).",
)
@click.option("--respect-robots/--ignore-robots", default=False)
@click.option(
    "--block-media/--no-block-media",
    default=True,
    show_default=True,
    help="Block mp4/webm/etc. requests to keep crawling moving.",
)
@click.option("--no-tui", "no_tui", is_flag=True, default=False, help="Run headless.")
@click.option("--headed", is_flag=True, default=False, help="Show the Chromium window.")
@click.option(
    "--max-pages",
    type=int,
    default=0,
    show_default=True,
    help="Stop after this many pages have been completed (0 = unlimited).",
)
@click.option(
    "--block-host",
    multiple=True,
    metavar="HOST",
    help="Additional host(s) to skip (repeatable). Matches exact host or any "
    "subdomain. Default block list already includes discord.gg, t.me, zoom.us, "
    "slack.com, etc. — sites whose preview pages invoke OS protocol handlers.",
)
@click.option(
    "--no-default-blocklist",
    is_flag=True,
    default=False,
    help="Don't apply the built-in blocklist (use only hosts from --block-host).",
)
@click.option(
    "--report-only",
    default=None,
    help="Skip crawling: just (re)build the HTML report for the given run-id.",
)
@click.option(
    "--rescan-html",
    default=None,
    help="For the given run-id: re-extract links from already-captured HTML "
    "on disk (e.g. after upgrading the extractor) and enqueue any new "
    "discoveries. Then exit — resume the crawl normally to fetch them.",
)
@click.option("-v", "--verbose", is_flag=True)
def main(
    seed: Optional[str],
    run_id: Optional[str],
    out: str,
    concurrency: int,
    politeness_ms: int,
    strict_host: bool,
    respect_robots: bool,
    block_media: bool,
    no_tui: bool,
    headed: bool,
    max_pages: int,
    block_host: tuple[str, ...],
    no_default_blocklist: bool,
    report_only: Optional[str],
    rescan_html: Optional[str],
    verbose: bool,
) -> None:
    """schizospider — crawl weird websites, screenshot every page, produce an HTML report."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if report_only:
        # Need a Settings just to find the run dir + seed.
        out_root = Path(out).resolve()
        run_dir = out_root / report_only
        if not (run_dir / "db.sqlite").exists():
            click.echo(f"no db at {run_dir / 'db.sqlite'}", err=True)
            sys.exit(2)
        # Pull seed from meta.
        import sqlite3

        conn = sqlite3.connect(str(run_dir / "db.sqlite"))
        conn.row_factory = sqlite3.Row
        seed_row = conn.execute("SELECT value FROM meta WHERE key='seed'").fetchone()
        conn.close()
        seed = seed_row["value"] if seed_row else "about:blank"
        settings = _make_settings(
            seed=seed,
            run_id=report_only,
            out=out,
            concurrency=concurrency,
            politeness_ms=politeness_ms,
            host_mode="strict" if strict_host else "registrable",
            respect_robots=respect_robots,
            block_media=block_media,
            headless=not headed,
        )

        async def _build() -> None:
            bus = Bus()
            store = Store(
                settings.db_path,
                seed_url=settings.seed,
                host_mode=settings.host_mode,
                bus=bus,
            )
            await store.open()
            path = await build_report(settings, store)
            await store.close()
            click.echo(f"report: {path}")

        asyncio.run(_build())
        return

    if rescan_html:
        asyncio.run(_rescan_html(rescan_html, out, strict_host))
        return

    if not seed:
        click.echo("error: --seed is required (or use --report-only).", err=True)
        sys.exit(2)

    # Compose effective blocklist: built-in defaults (unless suppressed) plus
    # any user-supplied --block-host values.
    from schizospider.config import DEFAULT_BLOCKED_HOSTS
    effective_blocked: tuple[str, ...] = tuple(
        sorted({
            *( () if no_default_blocklist else DEFAULT_BLOCKED_HOSTS ),
            *(h.lower().strip() for h in block_host if h.strip()),
        })
    )

    settings = _make_settings(
        seed=seed,
        run_id=run_id,
        out=out,
        concurrency=concurrency,
        politeness_ms=politeness_ms,
        host_mode="strict" if strict_host else "registrable",
        respect_robots=respect_robots,
        block_media=block_media,
        headless=not headed,
        max_pages=max_pages,
        blocked_hosts=effective_blocked,
    )

    click.echo(f"seed:     {settings.seed}")
    click.echo(f"run-id:   {settings.run_id}")
    click.echo(f"out:      {settings.run_dir}")
    click.echo(f"workers:  {settings.concurrency}  politeness: {settings.politeness_ms}ms")
    click.echo(f"host:     {settings.host_mode}")
    if settings.max_pages > 0:
        click.echo(f"max:      {settings.max_pages} pages")
    if settings.blocked_hosts:
        click.echo(f"blocked:  {len(settings.blocked_hosts)} hosts ({', '.join(settings.blocked_hosts[:5])}{'...' if len(settings.blocked_hosts) > 5 else ''})")
    click.echo("")

    exit_code = 0
    try:
        asyncio.run(_run_crawl(settings, use_tui=not no_tui))
    except KeyboardInterrupt:
        click.echo("interrupted", err=True)
        exit_code = 130
    # Playwright's subprocess transports on Windows linger past asyncio.run()
    # returning, leaving the Python process alive with nothing actionable to do.
    # Once the report is built and the store is closed, force-exit so the user
    # gets their shell prompt back instead of staring at a frozen terminal.
    import os
    os._exit(exit_code)


if __name__ == "__main__":
    main()
