from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Hosts whose preview pages typically invoke a native protocol handler on the
# OS (e.g. Discord's preview JS calls `discord://invite/...`), which Windows
# will then route to the installed desktop app even from headless Chromium.
# Fetching these is rarely useful and surprises users with popups. Default-deny.
DEFAULT_BLOCKED_HOSTS: tuple[str, ...] = (
    "discord.gg",
    "discord.com",       # discord.com/invite/<code>
    "discordapp.com",
    "t.me",              # Telegram invites
    "telegram.me",
    "telegram.org",
    "join.skype.com",
    "zoom.us",           # zoom.us/j/<id> → zoommtg:// launch
    "teams.microsoft.com",
    "slack.com",         # workspace.slack.com slack:// launches
)


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
    # Hosts to skip outright (URL never enqueued). Default includes Discord /
    # Telegram / Zoom / Slack invite domains, which trigger OS protocol-handler
    # popups even when fetched from headless Chromium.
    blocked_hosts: tuple[str, ...] = DEFAULT_BLOCKED_HOSTS

    def is_host_blocked(self, host: str) -> bool:
        """Return True if `host` is on the blocklist (exact or subdomain match)."""
        if not host or not self.blocked_hosts:
            return False
        host = host.lower()
        for blocked in self.blocked_hosts:
            b = blocked.lower()
            if host == b or host.endswith("." + b):
                return True
        return False

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
