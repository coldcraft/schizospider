"""Capture screenshots of an existing report for the README.

Usage:  python scripts/capture_docs_screenshots.py <run-dir>

Writes PNGs into docs/screenshots/.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main(run_dir: Path) -> None:
    out_dir = Path("docs/screenshots")
    out_dir.mkdir(parents=True, exist_ok=True)

    report_url = (run_dir / "report.html").resolve().as_uri()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1600, "height": 1000})
        page = await ctx.new_page()

        # Grid view
        await page.goto(report_url, wait_until="domcontentloaded")
        await page.wait_for_function("window.__SCHIZO_DATA__ != null", timeout=15_000)
        await page.wait_for_selector(".card", timeout=15_000)
        await page.wait_for_timeout(800)
        await page.screenshot(path=str(out_dir / "report-grid.png"), full_page=False)
        print(f"wrote {out_dir / 'report-grid.png'}")

        # Graph view
        await page.click("button.tab[data-view='graph']")
        # Give vis-network time to stabilize.
        await page.wait_for_selector("#graph canvas", timeout=15_000)
        await page.wait_for_timeout(4_000)
        await page.screenshot(path=str(out_dir / "report-graph.png"), full_page=False)
        print(f"wrote {out_dir / 'report-graph.png'}")

        # Detail view — click first card.
        await page.click("button.tab[data-view='grid']")
        await page.wait_for_selector(".card", timeout=10_000)
        first = await page.query_selector("a.cardlink")
        if first:
            href = await first.get_attribute("href")
            if href:
                detail_url = (run_dir / href).resolve().as_uri()
                await page.goto(detail_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(800)
                await page.screenshot(
                    path=str(out_dir / "report-detail.png"), full_page=False
                )
                print(f"wrote {out_dir / 'report-detail.png'}")

        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    asyncio.run(main(Path(sys.argv[1])))
