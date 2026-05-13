from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def default_run_id(seed: str) -> str:
    from urllib.parse import urlsplit

    host = urlsplit(seed).netloc.replace(":", "_") or "run"
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{host}"


@dataclass(frozen=True)
class Settings:
    seed: str
    run_id: str
    out_root: Path
    concurrency: int = 4
    politeness_ms: int = 250
    nav_timeout_ms: int = 25_000
    host_mode: str = "registrable"   # "registrable" | "strict"
    respect_robots: bool = False
    block_media: bool = True
    user_agent: str = (
        "Mozilla/5.0 (compatible; schizospider/0.1; "
        "+https://github.com/coldcraft/schizospider)"
    )
    max_html_bytes: int = 5 * 1024 * 1024
    max_screenshot_height_px: int = 32_000
    headless: bool = True
    max_pages: int = 0  # 0 = unlimited

    @property
    def run_dir(self) -> Path:
        return self.out_root / self.run_id

    @property
    def db_path(self) -> Path:
        return self.run_dir / "db.sqlite"

    @property
    def screenshots_dir(self) -> Path:
        return self.run_dir / "screenshots"

    @property
    def pages_dir(self) -> Path:
        return self.run_dir / "pages"

    def ensure_dirs(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.pages_dir.mkdir(parents=True, exist_ok=True)
