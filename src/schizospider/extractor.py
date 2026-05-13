from __future__ import annotations

import re
from typing import Any

from schizospider.urls import (
    canonicalize,
    classify,
    extract_js_urls,
    extract_meta_refresh,
)


# JS snippet executed inside each frame to harvest link-like elements.
_HARVEST_JS = r"""
() => {
    const out = [];
    const push = (href, text, tag) => {
        if (!href) return;
        out.push({href: String(href), text: (text || "").trim().slice(0, 256), tag: tag});
    };
    for (const a of document.querySelectorAll("a[href]")) {
        push(a.getAttribute("href"), a.innerText || a.textContent, "a");
    }
    for (const a of document.querySelectorAll("area[href]")) {
        push(a.getAttribute("href"), a.getAttribute("alt"), "area");
    }
    for (const f of document.querySelectorAll("frame[src]")) {
        push(f.getAttribute("src"), f.getAttribute("name"), "frame");
    }
    for (const f of document.querySelectorAll("iframe[src]")) {
        push(f.getAttribute("src"), f.getAttribute("title"), "iframe");
    }
    for (const l of document.querySelectorAll("link[rel='alternate'][href], link[rel='canonical'][href]")) {
        push(l.getAttribute("href"), l.getAttribute("rel"), "link");
    }
    // meta refresh
    for (const m of document.querySelectorAll("meta[http-equiv]")) {
        const eq = (m.getAttribute("http-equiv") || "").toLowerCase();
        if (eq === "refresh") {
            push(m.getAttribute("content"), "meta-refresh", "meta-refresh");
        }
    }
    // inline <script> bodies -- caller scans these with regex
    const scripts = [];
    for (const s of document.querySelectorAll("script:not([src])")) {
        const txt = s.textContent || "";
        if (txt.length < 4000) scripts.push(txt);
    }
    return {links: out, scripts: scripts, frame_url: location.href, title: document.title || ""};
}
"""


async def extract_from_page(page: Any) -> tuple[str, list[tuple[str, str, str]]]:
    """Return (title, outlinks) for a Playwright Page.

    outlinks is a list of (resolved_url, anchor_text, link_kind).
    Walks every frame (including nested framesets). Resolves relative URLs
    against the frame's own URL, then canonicalizes for dedup.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str]] = []
    title = ""

    for frame in page.frames:
        try:
            harvest = await frame.evaluate(_HARVEST_JS)
        except Exception:
            continue
        if not isinstance(harvest, dict):
            continue
        frame_url = harvest.get("frame_url") or page.url
        if not title:
            title = (harvest.get("title") or "").strip()

        for item in harvest.get("links") or []:
            href = item.get("href")
            text = item.get("text") or ""
            tag = item.get("tag") or "a"
            _absorb(href, text, tag, frame_url, seen, out)

            # meta-refresh "content" attribute carries "5; url=foo"
            if tag == "meta-refresh":
                refresh = extract_meta_refresh(href or "")
                if refresh:
                    _absorb(refresh, "meta-refresh", "meta-refresh", frame_url, seen, out)

        # Scan inline scripts for window.open / location.href URLs.
        for script in harvest.get("scripts") or []:
            for js_url in extract_js_urls(script):
                _absorb(js_url, "(js)", "js", frame_url, seen, out)

    return title, out


def _absorb(
    href: str | None,
    text: str,
    tag: str,
    base: str,
    seen: set[tuple[str, str]],
    out: list[tuple[str, str, str]],
) -> None:
    if not href:
        return
    href = href.strip()
    if not href or href.startswith("#"):
        return

    # `javascript:...` — extract embedded URLs if any.
    if href.lower().startswith("javascript:"):
        for js_url in extract_js_urls(href):
            _absorb(js_url, text, "js", base, seen, out)
        return

    kind = classify(href)
    if kind == "invalid":
        return
    if kind == "external_scheme":
        # mailto:, tel:, data:, etc. — record as-is so the report can show them.
        key = (href, tag)
        if key in seen:
            return
        seen.add(key)
        out.append((href, text, "external_scheme"))
        return

    canon = canonicalize(href, base)
    if not canon:
        return
    key = (canon, tag)
    if key in seen:
        return
    seen.add(key)
    out.append((canon, text, tag))


# Used by tests: extract from raw HTML when we don't have a live Playwright frame.
def extract_from_html(html: str, base_url: str) -> list[tuple[str, str, str]]:
    """Lightweight regex-based fallback for tests. Real crawl uses extract_from_page."""
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    # Use backreference so the captured attribute value can contain the *other* quote type
    # (e.g. <a href="javascript:window.open('foo.html')">).
    a_rx     = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*(['"])(.*?)\1[^>]*?>(.*?)</a>""", re.I | re.S)
    area_rx  = re.compile(r"""<area\b[^>]*?\bhref\s*=\s*(['"])(.*?)\1""", re.I)
    frame_rx = re.compile(r"""<frame\b[^>]*?\bsrc\s*=\s*(['"])(.*?)\1""", re.I)
    iframe_rx = re.compile(r"""<iframe\b[^>]*?\bsrc\s*=\s*(['"])(.*?)\1""", re.I)

    for m in a_rx.finditer(html):
        _absorb(m.group(2), _strip_tags(m.group(3)), "a", base_url, seen, out)
    for m in area_rx.finditer(html):
        _absorb(m.group(2), "", "area", base_url, seen, out)
    for m in frame_rx.finditer(html):
        _absorb(m.group(2), "", "frame", base_url, seen, out)
    for m in iframe_rx.finditer(html):
        _absorb(m.group(2), "", "iframe", base_url, seen, out)

    # Inline <script> bodies.
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.I | re.S):
        for js_url in extract_js_urls(m.group(1)):
            _absorb(js_url, "(js)", "js", base_url, seen, out)

    return out


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()[:256]
