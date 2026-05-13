from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from schizospider.events import Bus
from schizospider.urls import canonicalize, registrable_domain, same_site

# Page lifecycle states.
QUEUED = "queued"
IN_FLIGHT = "in_flight"
DONE = "done"
ERROR = "error"
SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY,
  url_canonical      TEXT UNIQUE NOT NULL,
  url_original       TEXT NOT NULL,
  registrable_domain TEXT NOT NULL,
  is_seed_domain     INTEGER NOT NULL,
  state              TEXT NOT NULL,
  depth              INTEGER NOT NULL,
  http_status        INTEGER,
  content_type       TEXT,
  title              TEXT,
  screenshot_path    TEXT,
  html_path          TEXT,
  headers_json       TEXT,
  error              TEXT,
  attempts           INTEGER NOT NULL DEFAULT 0,
  enqueued_at REAL,
  started_at  REAL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS ix_pages_state  ON pages(state);
CREATE INDEX IF NOT EXISTS ix_pages_domain ON pages(registrable_domain);

CREATE TABLE IF NOT EXISTS links (
  src_id      INTEGER NOT NULL REFERENCES pages(id),
  dst_id      INTEGER NOT NULL REFERENCES pages(id),
  anchor_text TEXT,
  link_kind   TEXT NOT NULL,
  PRIMARY KEY (src_id, dst_id, link_kind)
);
CREATE INDEX IF NOT EXISTS ix_links_dst ON links(dst_id);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def sha16(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


@dataclass
class PageRow:
    id: int
    url_canonical: str
    url_original: str
    registrable_domain: str
    is_seed_domain: bool
    state: str
    depth: int
    http_status: Optional[int]
    content_type: Optional[str]
    title: Optional[str]
    screenshot_path: Optional[str]
    html_path: Optional[str]
    headers_json: Optional[str]
    error: Optional[str]
    attempts: int


@dataclass
class FetchResult:
    final_url: str
    http_status: Optional[int]
    content_type: Optional[str]
    title: Optional[str]
    headers: dict[str, str]
    html: Optional[str]
    screenshot_bytes: Optional[bytes]
    outlinks: list[tuple[str, str, str]]  # (resolved_url, anchor_text, link_kind)
    error: Optional[str] = None


def _row_to_page(row: sqlite3.Row) -> PageRow:
    return PageRow(
        id=row["id"],
        url_canonical=row["url_canonical"],
        url_original=row["url_original"],
        registrable_domain=row["registrable_domain"],
        is_seed_domain=bool(row["is_seed_domain"]),
        state=row["state"],
        depth=row["depth"],
        http_status=row["http_status"],
        content_type=row["content_type"],
        title=row["title"],
        screenshot_path=row["screenshot_path"],
        html_path=row["html_path"],
        headers_json=row["headers_json"],
        error=row["error"],
        attempts=row["attempts"],
    )


class Store:
    """SQLite DAO. All DB work runs on a single dedicated thread via to_thread.

    SQLite is fine with multiple readers + one writer in WAL mode, but we get
    cleaner ordering by funneling writes through one connection.
    """

    def __init__(self, db_path: Path, seed_url: str, host_mode: str, bus: Optional[Bus] = None):
        self.db_path = db_path
        self.seed_url = seed_url
        self.seed_registrable = registrable_domain(seed_url)
        self.host_mode = host_mode
        self.bus = bus
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ---------- lifecycle ----------

    async def open(self) -> None:
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        # Requeue in-flight rows from a previous interrupted run.
        conn.execute(
            "UPDATE pages SET state=? WHERE state=?",
            (QUEUED, IN_FLIGHT),
        )
        self._conn = conn

    async def close(self) -> None:
        if self._conn:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    # ---------- meta ----------

    async def set_meta(self, key: str, value: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_meta_sync, key, value)

    def _set_meta_sync(self, key: str, value: str) -> None:
        assert self._conn
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def get_meta(self, key: str) -> Optional[str]:
        return await asyncio.to_thread(self._get_meta_sync, key)

    def _get_meta_sync(self, key: str) -> Optional[str]:
        assert self._conn
        cur = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    # ---------- enqueue ----------

    async def enqueue_seed(self) -> int:
        canon = canonicalize(self.seed_url)
        if not canon:
            raise ValueError(f"Cannot canonicalize seed: {self.seed_url}")
        return await self.enqueue(canon, depth=0, original=self.seed_url)

    async def enqueue(self, url: str, depth: int, original: Optional[str] = None) -> Optional[int]:
        canon = canonicalize(url)
        if not canon:
            return None
        async with self._lock:
            row_id = await asyncio.to_thread(self._enqueue_sync, canon, depth, original or url)
        if row_id is not None and self.bus:
            self.bus.publish("page_enqueued", row_id)
        return row_id

    def _enqueue_sync(self, canon: str, depth: int, original: str) -> Optional[int]:
        assert self._conn
        is_seed = 1 if same_site(canon, self.seed_url, self.host_mode) else 0
        rdom = registrable_domain(canon)
        cur = self._conn.execute(
            "INSERT INTO pages "
            "(url_canonical, url_original, registrable_domain, is_seed_domain, state, depth, enqueued_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url_canonical) DO NOTHING",
            (canon, original, rdom, is_seed, QUEUED, depth, time.time()),
        )
        if cur.rowcount == 0:
            return None
        return cur.lastrowid

    async def enqueue_many(
        self,
        outlinks: Iterable[tuple[str, str, str]],
        depth: int,
        src_id: Optional[int] = None,
        on_domain_only: bool = False,
    ) -> list[int]:
        """Insert pages + link edges. Returns newly created page IDs.

        outlinks: iterable of (resolved_url, anchor_text, link_kind).
        If `on_domain_only`, off-domain targets are still recorded as pages
        (state=queued) so they get fetched once; the caller decides not to
        recurse via depth logic in crawler.
        """
        new_ids: list[int] = []
        async with self._lock:
            new_ids = await asyncio.to_thread(
                self._enqueue_many_sync, list(outlinks), depth, src_id, on_domain_only
            )
        if self.bus:
            for nid in new_ids:
                self.bus.publish("page_enqueued", nid)
            if src_id is not None:
                self.bus.publish("page_updated", src_id)
        return new_ids

    def _enqueue_many_sync(
        self,
        outlinks: list[tuple[str, str, str]],
        depth: int,
        src_id: Optional[int],
        on_domain_only: bool,
    ) -> list[int]:
        assert self._conn
        new_ids: list[int] = []
        for resolved, anchor, kind in outlinks:
            canon = canonicalize(resolved)
            if not canon:
                continue
            is_seed = same_site(canon, self.seed_url, self.host_mode)
            rdom = registrable_domain(canon)
            cur = self._conn.execute(
                "INSERT INTO pages "
                "(url_canonical, url_original, registrable_domain, is_seed_domain, state, depth, enqueued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(url_canonical) DO NOTHING",
                (canon, resolved, rdom, 1 if is_seed else 0, QUEUED, depth, time.time()),
            )
            if cur.rowcount:
                new_ids.append(cur.lastrowid)
                dst_id: Optional[int] = cur.lastrowid
            else:
                # Row already exists. If it was previously inserted as a
                # `skipped` graph leaf (because some off-domain page mentioned
                # it first), promote it back to `queued` now that a stronger
                # discovery channel — enqueue_many — wants it crawled.
                row = self._conn.execute(
                    "SELECT id, state FROM pages WHERE url_canonical=?",
                    (canon,),
                ).fetchone()
                dst_id = row["id"] if row else None
                if row and row["state"] == SKIPPED:
                    self._conn.execute(
                        "UPDATE pages SET state=?, depth=?, enqueued_at=? "
                        "WHERE id=? AND state=?",
                        (QUEUED, depth, time.time(), row["id"], SKIPPED),
                    )
                    new_ids.append(row["id"])
            if src_id is not None and dst_id is not None:
                self._conn.execute(
                    "INSERT INTO links (src_id, dst_id, anchor_text, link_kind) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(src_id, dst_id, link_kind) DO NOTHING",
                    (src_id, dst_id, (anchor or "")[:512], kind),
                )
        return new_ids

    async def record_links_only(
        self, src_id: int, outlinks: Iterable[tuple[str, str, str]]
    ) -> None:
        """Off-domain pages: store the outlinks as graph edges but do NOT enqueue them.

        We create lightweight page rows (state='skipped') for any target we
        haven't already seen, so the graph has a node to point at.
        """
        async with self._lock:
            await asyncio.to_thread(self._record_links_only_sync, src_id, list(outlinks))
        if self.bus:
            self.bus.publish("page_updated", src_id)

    def _record_links_only_sync(
        self, src_id: int, outlinks: list[tuple[str, str, str]]
    ) -> None:
        assert self._conn
        for resolved, anchor, kind in outlinks:
            canon = canonicalize(resolved)
            if not canon:
                continue
            rdom = registrable_domain(canon)
            is_seed = same_site(canon, self.seed_url, self.host_mode)
            cur = self._conn.execute(
                "INSERT INTO pages "
                "(url_canonical, url_original, registrable_domain, is_seed_domain, state, depth) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(url_canonical) DO NOTHING",
                (canon, resolved, rdom, 1 if is_seed else 0, SKIPPED, 99),
            )
            if cur.rowcount:
                dst_id = cur.lastrowid
            else:
                row = self._conn.execute(
                    "SELECT id FROM pages WHERE url_canonical=?", (canon,)
                ).fetchone()
                dst_id = row["id"] if row else None
            if dst_id is not None:
                self._conn.execute(
                    "INSERT INTO links (src_id, dst_id, anchor_text, link_kind) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(src_id, dst_id, link_kind) DO NOTHING",
                    (src_id, dst_id, (anchor or "")[:512], kind),
                )

    # ---------- lease / complete / fail ----------

    async def lease_next(self) -> Optional[PageRow]:
        async with self._lock:
            return await asyncio.to_thread(self._lease_next_sync)

    def _lease_next_sync(self) -> Optional[PageRow]:
        assert self._conn
        # Prefer shallower depth, FIFO within depth.
        row = self._conn.execute(
            "SELECT * FROM pages WHERE state=? "
            "ORDER BY depth ASC, id ASC LIMIT 1",
            (QUEUED,),
        ).fetchone()
        if not row:
            return None
        self._conn.execute(
            "UPDATE pages SET state=?, started_at=?, attempts=attempts+1 WHERE id=?",
            (IN_FLIGHT, time.time(), row["id"]),
        )
        # Re-fetch to reflect new state.
        row = self._conn.execute(
            "SELECT * FROM pages WHERE id=?", (row["id"],)
        ).fetchone()
        return _row_to_page(row)

    async def complete(
        self,
        page_id: int,
        result: FetchResult,
        screenshot_rel: Optional[str],
        html_rel: Optional[str],
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._complete_sync, page_id, result, screenshot_rel, html_rel
            )
        if self.bus:
            self.bus.publish("page_done", page_id)

    def _complete_sync(
        self,
        page_id: int,
        result: FetchResult,
        screenshot_rel: Optional[str],
        html_rel: Optional[str],
    ) -> None:
        assert self._conn
        self._conn.execute(
            "UPDATE pages SET "
            "state=?, http_status=?, content_type=?, title=?, "
            "screenshot_path=?, html_path=?, headers_json=?, finished_at=?, error=NULL "
            "WHERE id=?",
            (
                DONE,
                result.http_status,
                result.content_type,
                (result.title or "")[:1000],
                screenshot_rel,
                html_rel,
                json.dumps(result.headers, ensure_ascii=False),
                time.time(),
                page_id,
            ),
        )

    async def fail(self, page_id: int, error: str, retryable: bool = True) -> None:
        async with self._lock:
            await asyncio.to_thread(self._fail_sync, page_id, error, retryable)
        if self.bus:
            self.bus.publish("page_failed", page_id)

    def _fail_sync(self, page_id: int, error: str, retryable: bool) -> None:
        assert self._conn
        row = self._conn.execute(
            "SELECT attempts FROM pages WHERE id=?", (page_id,)
        ).fetchone()
        attempts = row["attempts"] if row else 0
        # Allow up to 3 attempts; after that mark error permanently.
        if retryable and attempts < 3:
            self._conn.execute(
                "UPDATE pages SET state=?, error=?, finished_at=? WHERE id=?",
                (QUEUED, error[:2000], time.time(), page_id),
            )
        else:
            self._conn.execute(
                "UPDATE pages SET state=?, error=?, finished_at=? WHERE id=?",
                (ERROR, error[:2000], time.time(), page_id),
            )

    # ---------- queries ----------

    async def queue_size(self) -> int:
        return await asyncio.to_thread(self._count_state, QUEUED)

    async def in_flight_size(self) -> int:
        return await asyncio.to_thread(self._count_state, IN_FLIGHT)

    async def done_count(self) -> int:
        return await asyncio.to_thread(self._count_state, DONE)

    async def error_count(self) -> int:
        return await asyncio.to_thread(self._count_state, ERROR)

    def _count_state(self, state: str) -> int:
        assert self._conn
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM pages WHERE state=?", (state,)
        ).fetchone()
        return row["n"] if row else 0

    async def total_count(self) -> int:
        return await asyncio.to_thread(self._total_count_sync)

    def _total_count_sync(self) -> int:
        assert self._conn
        row = self._conn.execute("SELECT COUNT(*) AS n FROM pages").fetchone()
        return row["n"] if row else 0

    async def get_page(self, page_id: int) -> Optional[PageRow]:
        return await asyncio.to_thread(self._get_page_sync, page_id)

    def _get_page_sync(self, page_id: int) -> Optional[PageRow]:
        assert self._conn
        row = self._conn.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
        return _row_to_page(row) if row else None

    async def list_pages(self, limit: int = 5000) -> list[PageRow]:
        return await asyncio.to_thread(self._list_pages_sync, limit)

    def _list_pages_sync(self, limit: int) -> list[PageRow]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM pages ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_page(r) for r in rows]

    async def list_links(self) -> list[tuple[int, int, str, str]]:
        return await asyncio.to_thread(self._list_links_sync)

    def _list_links_sync(self) -> list[tuple[int, int, str, str]]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT src_id, dst_id, link_kind, COALESCE(anchor_text,'') AS anchor "
            "FROM links"
        ).fetchall()
        return [(r["src_id"], r["dst_id"], r["link_kind"], r["anchor"]) for r in rows]

    async def inbound_links(self, page_id: int) -> list[tuple[int, str]]:
        return await asyncio.to_thread(self._inbound_links_sync, page_id)

    def _inbound_links_sync(self, page_id: int) -> list[tuple[int, str]]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT src_id, COALESCE(anchor_text,'') AS anchor FROM links WHERE dst_id=?",
            (page_id,),
        ).fetchall()
        return [(r["src_id"], r["anchor"]) for r in rows]

    async def outbound_links(self, page_id: int) -> list[tuple[int, str, str]]:
        return await asyncio.to_thread(self._outbound_links_sync, page_id)

    def _outbound_links_sync(self, page_id: int) -> list[tuple[int, str, str]]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT dst_id, link_kind, COALESCE(anchor_text,'') AS anchor "
            "FROM links WHERE src_id=?",
            (page_id,),
        ).fetchall()
        return [(r["dst_id"], r["link_kind"], r["anchor"]) for r in rows]
