from __future__ import annotations

import asyncio
import json
import re
import shutil
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, unquote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from schizospider.config import Settings
from schizospider.store import PageRow, Store

log = logging.getLogger("schizospider.report")

# Thumbnail dimensions for the grid. Keeps DOM lightweight even at 2k+ pages.
THUMB_MAX_W = 480
THUMB_MAX_H = 360


def _templates_dir() -> Path:
    return Path(__file__).parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_templates_dir())),
        autoescape=select_autoescape(["html", "htm", "j2"]),
        enable_async=False,
    )


def _title_fallback(p: PageRow) -> str:
    """Show a sensible label when <title> is missing — use URL path basename."""
    if p.title and p.title.strip():
        return p.title.strip()
    try:
        path = urlsplit(p.url_canonical).path
        leaf = path.rstrip("/").rsplit("/", 1)[-1] or "/"
        return unquote(leaf) or "(untitled)"
    except Exception:
        return "(untitled)"


def _has_renderable_content(p: PageRow) -> bool:
    """Pages worth a detail file: anything we actually fetched."""
    return p.state in ("done", "error") and (
        p.screenshot_path or p.html_path or p.http_status is not None or p.error
    )


_HEAD_RE = re.compile(r"<head\b[^>]*>", re.I)
_HTML_RE = re.compile(r"<html\b[^>]*>", re.I)
_BASE_INJECTED_RE = re.compile(
    r"<base\b[^>]*\bhref=\"[^\"]*\">", re.I
)


def _strip_injected_base(html_file: Path) -> None:
    """Undo the in-place <base> injection from prior buggy builds.

    The earlier code mutated pages/<sha>.html in place — which meant opening
    that raw file in a browser made every script inside fetch from the live
    site. We now keep raw captures untouched and stage a sanitized copy
    elsewhere; this function reverts any pre-existing in-place injection.
    """
    try:
        content = html_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    new = _BASE_INJECTED_RE.sub("", content, count=1)
    if new != content:
        try:
            html_file.write_text(new, encoding="utf-8")
        except Exception:
            pass


def _make_iframe_safe_html(raw: str, original_url: str) -> str:
    """Return a copy of the captured page with `<base>` and a strict CSP.

    The CSP `script-src 'none'` neuters every inline + external script so
    the iframe shows the captured DOM without firing jodi.org's hostile JS.
    `<base>` makes relative URLs in framesets, images and stylesheets resolve
    against the original site (which the iframe will load over the network
    if you're online — same trade-off as an archive playback).
    """
    safe_url = original_url.replace('"', "&quot;")
    inject = (
        f'<meta http-equiv="Content-Security-Policy" '
        f'content="script-src \'none\'; object-src \'none\'">'
        f'<base href="{safe_url}">'
    )
    new = _HEAD_RE.sub(lambda m: m.group(0) + inject, raw, count=1)
    if new == raw:
        new = _HTML_RE.sub(
            lambda m: m.group(0) + f"<head>{inject}</head>", raw, count=1
        )
        if new == raw:
            new = f"<head>{inject}</head>" + raw
    return new


def _make_thumb(src_png: Path, dst_webp: Path) -> Optional[str]:
    """Generate a thumbnail. Returns the relative path on success, None on failure."""
    if dst_webp.exists():
        return None  # already there, caller treats as "use existing"
    try:
        from PIL import Image  # local import keeps Pillow optional at import time
    except ImportError:
        return None
    try:
        with Image.open(src_png) as img:
            img.thumbnail((THUMB_MAX_W, THUMB_MAX_H), Image.LANCZOS)
            # Convert to RGB if needed (webp doesn't always handle palette/A well)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            dst_webp.parent.mkdir(parents=True, exist_ok=True)
            img.save(dst_webp, format="WEBP", quality=72, method=4)
        return str(dst_webp)
    except Exception as e:
        log.debug("thumb failed for %s: %s", src_png, e)
        return None


async def build_report(settings: Settings, store: Store) -> Path:
    """Render report.html, per-page detail files, data.json, and copy assets."""
    settings.ensure_dirs()
    run_dir = settings.run_dir

    pages = await store.list_pages()
    links = await store.list_links()

    pages_by_id = {p.id: p for p in pages}

    out_by_src: dict[int, list[tuple[int, str, str]]] = {}
    in_by_dst: dict[int, list[tuple[int, str]]] = {}
    for src_id, dst_id, kind, anchor in links:
        out_by_src.setdefault(src_id, []).append((dst_id, kind, anchor))
        in_by_dst.setdefault(dst_id, []).append((src_id, anchor))

    # Generate thumbnails. Run *serially* in a single dedicated thread:
    # Pillow's WebP encoder is CPU-bound, and we're typically writing to a
    # network filesystem where parallel Pillow encodes contend on locks and
    # can deadlock the entire build under high page counts.
    thumbs_dir = run_dir / "thumbs"
    thumbs_dir.mkdir(exist_ok=True)
    thumb_rel_by_id: dict[int, Optional[str]] = {}

    def _do_thumbs_serial() -> None:
        for p in pages:
            if not p.screenshot_path:
                continue
            src = run_dir / p.screenshot_path
            if not src.exists():
                continue
            thumb_name = src.stem + ".webp"
            dst = thumbs_dir / thumb_name
            rel = f"thumbs/{thumb_name}"
            if dst.exists():
                thumb_rel_by_id[p.id] = rel
                continue
            try:
                _make_thumb(src, dst)
                if dst.exists():
                    thumb_rel_by_id[p.id] = rel
            except Exception as e:
                log.debug("thumb failed for page %d: %s", p.id, e)

    await asyncio.to_thread(_do_thumbs_serial)

    pages_json: list[dict[str, Any]] = []
    for p in pages:
        is_skipped = p.state == "skipped"
        thumb_rel = thumb_rel_by_id.get(p.id)
        pages_json.append(
            {
                "id": p.id,
                "url": p.url_canonical,
                "title": _title_fallback(p),
                "title_raw": p.title or "",
                "state": p.state,
                "status": p.http_status,
                "content_type": p.content_type,
                # depth=99 is our sentinel for "off-domain leaf, not crawled".
                # Emit null so the JS can render "off" instead of a misleading number.
                "depth": None if (is_skipped and p.depth >= 99) else p.depth,
                "domain": p.registrable_domain,
                "is_seed": int(bool(p.is_seed_domain)),
                "screenshot": p.screenshot_path,
                "thumb": thumb_rel or p.screenshot_path,
                "html_path": p.html_path,
                "out_count": len(out_by_src.get(p.id, [])),
                "in_count": len(in_by_dst.get(p.id, [])),
                "error": p.error,
                # Detail file only exists for pages we actually fetched.
                "detail_path": (
                    f"pages_view/{p.id}.html" if _has_renderable_content(p) else None
                ),
            }
        )

    links_json = [{"src": s, "dst": d, "kind": k} for (s, d, k, _anchor) in links]

    seed_domain = ""
    for p in pages:
        if p.is_seed_domain and p.depth == 0:
            seed_domain = p.registrable_domain
            break
    if not seed_domain and pages:
        seed_domain = pages[0].registrable_domain

    fetched_count = sum(1 for p in pages if p.state == "done")
    error_count = sum(1 for p in pages if p.state == "error")
    skipped_count = sum(1 for p in pages if p.state == "skipped")

    data = {
        "meta": {
            "seed": settings.seed,
            "run_id": settings.run_id,
            "seed_domain": seed_domain,
            "page_count": len(pages),
            "fetched_count": fetched_count,
            "error_count": error_count,
            "skipped_count": skipped_count,
            "link_count": len(links),
        },
        "pages": pages_json,
        "links": links_json,
    }

    (run_dir / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # JS-loadable form for file:// browsers that block fetch().
    data_js = "window.__SCHIZO_DATA__ = " + json.dumps(data, ensure_ascii=False) + ";"
    (run_dir / "data.js").write_text(data_js, encoding="utf-8")

    env = _env()

    report_tmpl = env.get_template("report.html.j2")
    (run_dir / "report.html").write_text(
        report_tmpl.render(meta=data["meta"]), encoding="utf-8"
    )

    # Only render detail files for pages we actually fetched. Skipped off-domain
    # leaves are just graph nodes; rendering a stub page each would mean 10x
    # the file count for a typical crawl.
    detail_tmpl = env.get_template("page.html.j2")
    pages_view_dir = run_dir / "pages_view"
    if pages_view_dir.exists():
        shutil.rmtree(pages_view_dir)
    pages_view_dir.mkdir()
    pages_safe_dir = run_dir / "pages_safe"
    if pages_safe_dir.exists():
        shutil.rmtree(pages_safe_dir)
    pages_safe_dir.mkdir()

    detailed_pages = [p for p in pages if _has_renderable_content(p)]
    for p in detailed_pages:
        captured_html: str | None = None
        captured_safe_rel: str | None = None
        if p.html_path:
            html_file = run_dir / p.html_path
            if html_file.exists():
                # Reverse any in-place <base> injection from older buggy builds.
                _strip_injected_base(html_file)
                try:
                    raw = html_file.read_text(encoding="utf-8", errors="replace")
                    captured_html = raw
                    # Produce a sanitized copy: <base> + CSP no-scripts. The raw
                    # file under pages/ is left as forensic evidence.
                    safe = _make_iframe_safe_html(raw, p.url_canonical)
                    safe_name = Path(p.html_path).name  # same sha-based filename
                    (pages_safe_dir / safe_name).write_text(safe, encoding="utf-8")
                    captured_safe_rel = f"../pages_safe/{safe_name}"
                except Exception:
                    captured_html = None

        outbound = []
        for dst_id, kind, anchor in out_by_src.get(p.id, []):
            dst = pages_by_id.get(dst_id)
            if not dst:
                continue
            dst_has_detail = _has_renderable_content(dst)
            outbound.append(
                {
                    "url": dst.url_canonical,
                    "title": _title_fallback(dst),
                    "kind": kind,
                    "anchor": anchor,
                    "detail_path": (
                        f"../pages_view/{dst.id}.html" if dst_has_detail else None
                    ),
                    "is_seed": bool(dst.is_seed_domain),
                    "skipped": dst.state == "skipped",
                }
            )

        inbound = []
        for src_id, anchor in in_by_dst.get(p.id, []):
            src = pages_by_id.get(src_id)
            if not src:
                continue
            src_has_detail = _has_renderable_content(src)
            inbound.append(
                {
                    "url": src.url_canonical,
                    "title": _title_fallback(src),
                    "anchor": anchor,
                    "detail_path": (
                        f"../pages_view/{src.id}.html" if src_has_detail else None
                    ),
                    "is_seed": bool(src.is_seed_domain),
                }
            )

        rendered = detail_tmpl.render(
            page=p,
            display_title=_title_fallback(p),
            screenshot=("../" + p.screenshot_path) if p.screenshot_path else None,
            captured_html=captured_html,
            captured_html_path=captured_safe_rel,
            captured_html_raw_path=("../" + p.html_path) if p.html_path else None,
            outbound=outbound,
            inbound=inbound,
            meta=data["meta"],
        )
        (pages_view_dir / f"{p.id}.html").write_text(rendered, encoding="utf-8")

    # Copy assets directory.
    assets_src = _templates_dir() / "assets"
    assets_dst = run_dir / "assets"
    if assets_dst.exists():
        shutil.rmtree(assets_dst)
    shutil.copytree(assets_src, assets_dst)

    return run_dir / "report.html"
