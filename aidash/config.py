"""Path discovery and shared configuration."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
DB_PATH = DATA_DIR / "usage.db"
PRICING_PATH = Path(__file__).resolve().parent / "pricing.json"


def claude_home() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def claude_projects_dir() -> Path:
    return claude_home() / "projects"


def cursor_global_storage() -> Path | None:
    """Cursor's globalStorage dir, per-platform."""
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Cursor" / "User" / "globalStorage")
    home = Path.home()
    candidates.append(home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage")
    candidates.append(home / ".config" / "Cursor" / "User" / "globalStorage")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def load_pricing() -> dict:
    with open(PRICING_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def parse_ts(value):
    """Parse an ISO-8601 timestamp (or epoch ms) into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def local_parts(dt):
    """(iso_utc, local_date, local_hour, local_weekday) for a UTC datetime."""
    if dt is None:
        return None, None, None, None
    loc = dt.astimezone()
    return dt.isoformat(), loc.strftime("%Y-%m-%d"), loc.hour, loc.weekday()


def friendly_project(cwd: str | None, encoded_dir: str | None) -> str:
    """Human-readable project label from a cwd path.

    The encoded directory name (``C--ai-tests-claude-jarvis-v0-1``) can't be
    decoded back into a path - '-' is used for both separators and literal
    hyphens - so it is returned verbatim rather than guessed at. Real labels
    come from ``cwd``; see normalize_projects() in claude_ingest.
    """
    if cwd:
        norm = cwd.replace("\\", "/").rstrip("/")
        if norm:
            return norm.rsplit("/", 1)[-1] or norm
    if encoded_dir:
        return encoded_dir
    return "unknown"
