from pathlib import Path

import pytest

from schizospider.events import Bus
from schizospider.store import DONE, IN_FLIGHT, QUEUED, Store, FetchResult


@pytest.fixture
async def store(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    s = Store(db, seed_url="https://wwwwwwwww.jodi.org/", host_mode="registrable", bus=Bus())
    await s.open()
    yield s
    await s.close()


async def test_enqueue_dedup(store: Store):
    a = await store.enqueue("https://wwwwwwwww.jodi.org/foo", depth=1)
    b = await store.enqueue("https://wwwwwwwww.jodi.org/foo", depth=1)
    assert a is not None
    assert b is None


async def test_seed_is_on_domain(store: Store):
    await store.enqueue_seed()
    pages = await store.list_pages()
    assert len(pages) == 1
    assert pages[0].is_seed_domain is True


async def test_lease_complete(store: Store):
    pid = await store.enqueue("https://wwwwwwwww.jodi.org/x", depth=0)
    row = await store.lease_next()
    assert row is not None
    assert row.state == IN_FLIGHT
    assert row.id == pid

    result = FetchResult(
        final_url="https://wwwwwwwww.jodi.org/x",
        http_status=200,
        content_type="text/html",
        title="x",
        headers={"content-type": "text/html"},
        html="<html></html>",
        screenshot_bytes=b"PNGDATA",
        outlinks=[],
    )
    await store.complete(pid, result, screenshot_rel="screenshots/abc.png", html_rel="pages/abc.html")
    p = await store.get_page(pid)
    assert p.state == DONE
    assert p.http_status == 200
    assert p.screenshot_path == "screenshots/abc.png"


async def test_resume_requeues_in_flight(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    s1 = Store(db, seed_url="https://wwwwwwwww.jodi.org/", host_mode="registrable", bus=Bus())
    await s1.open()
    pid = await s1.enqueue("https://wwwwwwwww.jodi.org/a", depth=0)
    row = await s1.lease_next()
    assert row.state == IN_FLIGHT
    await s1.close()

    # Re-open store: in_flight row should now be queued again.
    s2 = Store(db, seed_url="https://wwwwwwwww.jodi.org/", host_mode="registrable", bus=Bus())
    await s2.open()
    p = await s2.get_page(pid)
    assert p.state == QUEUED
    await s2.close()


async def test_enqueue_many_records_links(store: Store):
    src = await store.enqueue("https://wwwwwwwww.jodi.org/", depth=0)
    new = await store.enqueue_many(
        [
            ("https://wwwwwwwww.jodi.org/a", "a", "a"),
            ("https://wwwwwwwww.jodi.org/b", "b", "a"),
        ],
        depth=1,
        src_id=src,
    )
    assert len(new) == 2
    links = await store.list_links()
    assert len(links) == 2
    assert all(link[0] == src for link in links)


async def test_record_links_only_does_not_recurse_targets(store: Store):
    src = await store.enqueue("https://cnn.com/", depth=1)
    await store.record_links_only(
        src,
        [("https://cnn.com/inner", "i", "a")],
    )
    pages = await store.list_pages()
    by_url = {p.url_canonical: p for p in pages}
    # inner page exists, but as skipped (won't be leased).
    assert "https://cnn.com/inner" in by_url
    assert by_url["https://cnn.com/inner"].state == "skipped"


async def test_blocked_hosts_are_skipped_never_queued(tmp_path):
    """URLs whose host is on the blocklist must never reach state=queued.

    Discord/Telegram/etc invite URLs trigger OS protocol-handler popups when
    fetched by headless Chromium — they must be recorded as graph edges but
    never actually visited.
    """
    db = tmp_path / "db.sqlite"
    s = Store(
        db,
        seed_url="https://example.org/",
        host_mode="registrable",
        bus=Bus(),
        blocked_hosts=("discord.gg", "discord.com"),
    )
    await s.open()
    src = await s.enqueue("https://example.org/", depth=0)
    new_ids = await s.enqueue_many(
        [
            ("https://discord.gg/AbCdEf", "join us", "a"),
            ("https://discord.com/invite/XyZ", "invite", "a"),
            ("https://images.discordapp.com/asset.png", "image", "a"),
            ("https://example.org/inner", "ok", "a"),
        ],
        depth=1,
        src_id=src,
    )
    pages = {p.url_canonical: p for p in await s.list_pages()}
    assert pages["https://discord.gg/AbCdEf"].state == "skipped"
    assert pages["https://discord.com/invite/XyZ"].state == "skipped"
    # Subdomain match still blocks (images.discordapp.com -> discordapp.com).
    # (Only true if discordapp.com is on the list — not in this test, so it must be queued.)
    assert pages["https://images.discordapp.com/asset.png"].state == "queued"
    # On-domain (or unrelated) URL still gets queued normally.
    assert pages["https://example.org/inner"].state == "queued"
    # The blocked targets are NOT in new_ids — they aren't pending work.
    blocked_ids = {
        pages["https://discord.gg/AbCdEf"].id,
        pages["https://discord.com/invite/XyZ"].id,
    }
    assert not (blocked_ids & set(new_ids))
    await s.close()


async def test_skipped_page_promoted_when_enqueued_later(store: Store):
    """A page first recorded as a skipped graph-leaf (because some off-domain page
    pointed at it) must be promoted to `queued` when an on-domain enqueue arrives.
    Without this, real cross-references get permanently buried."""
    # Off-domain page records a graph-leaf to some target — target ends up skipped.
    off_src = await store.enqueue("https://other.com/", depth=1)
    await store.record_links_only(
        off_src,
        [("https://target.example/page", "anchor", "a")],
    )
    pages = {p.url_canonical: p for p in await store.list_pages()}
    assert pages["https://target.example/page"].state == "skipped"

    # Now an on-domain page enqueues the same URL — it should be promoted.
    on_src = await store.enqueue("https://wwwwwwwww.jodi.org/", depth=0)
    new_ids = await store.enqueue_many(
        [("https://target.example/page", "anchor2", "a")],
        depth=1,
        src_id=on_src,
    )
    pages = {p.url_canonical: p for p in await store.list_pages()}
    assert pages["https://target.example/page"].state == "queued"
    assert pages["https://target.example/page"].depth == 1
    # The promoted row's id should be returned so callers can publish events.
    assert pages["https://target.example/page"].id in new_ids
