from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class Event:
    kind: str
    payload: Any = None


class Bus:
    """Minimal pub-sub. Subscribers get their own asyncio.Queue."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []

    def subscribe(self, maxsize: int = 1024) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def publish(self, kind: str, payload: Any = None) -> None:
        event = Event(kind=kind, payload=payload)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest on overflow to keep the TUI responsive.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
