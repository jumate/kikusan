"""Unavailable video cooldown management.

Tracks YouTube video IDs that returned "Video unavailable" errors and
prevents retrying them until a configurable cooldown period has elapsed.

FORK CHANGE: a video that fails in several consecutive cycles (default 3, each
cycle = one retry after the cooldown) is marked permanent: it is never retried
again and is listed in videos_no_disponibles.txt.

Storage: {data_dir}/unavailable.json
Format: {"video_id": {"failed_at": "ISO timestamp", "error": "error message",
                      "title": "optional title", "artist": "optional artist",
                      "attempts": 2, "first_failed_at": "ISO timestamp",
                      "permanent": false, "permanent_at": null}}

Environment variables (fork):
    KIKUSAN_UNAVAILABLE_MAX_ATTEMPTS     failed cycles before giving up (default 3, 0 = never give up)
    KIKUSAN_UNAVAILABLE_REPORT_FILE      path of the text report
                                         (default: next to the music, i.e. the parent of
                                         the .kikusan folder, as videos_no_disponibles.txt)
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kikusan.models.unavailable import UnavailableRecord

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_HOURS = 168  # 7 days

# --- Fork additions --------------------------------------------------------
DEFAULT_MAX_ATTEMPTS = 3      # failed cycles before a video is marked permanent
DEDUP_HOURS = 12              # failures closer than this are the SAME event (counted once)
STREAK_RESET_DAYS = 60        # a failure older than this no longer counts as "consecutive"
REPORT_FILENAME = "videos_no_disponibles.txt"
PERMANENT_REMAINING = timedelta(days=3650)  # what get_cooldown_remaining reports for permanent ones

# Patterns indicating the video itself is unavailable (not auth/network issues).
# Keep these specific: bare "This video is not available" is ambiguous and may be
# returned for transient extractor/client issues in some environments (e.g. Docker).
UNAVAILABLE_PATTERNS = [
    r"Video unavailable",
    r"This video is not available in your country",
    r"This video has been removed",
    r"This video is no longer available",
    r"This video does not exist",
    r"The uploader has not made this video available",
]


def is_unavailable_error(error_message: str) -> bool:
    """Check if an error message indicates a video is unavailable.

    Only matches errors where the video itself is unavailable, not
    auth errors, network issues, or other transient failures.

    Args:
        error_message: Error message string from yt-dlp

    Returns:
        True if the error indicates the video is unavailable
    """
    for pattern in UNAVAILABLE_PATTERNS:
        if re.search(pattern, error_message, re.IGNORECASE):
            return True
    return False


def get_unavailable_file(data_dir: Path) -> Path:
    """Get the path to the unavailable videos JSON file.

    Creates the parent directory if it doesn't exist.

    Args:
        data_dir: Data directory (e.g. ~/.kikusan)

    Returns:
        Path to unavailable.json
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "unavailable.json"


def load_unavailable(data_dir: Path) -> dict:
    """Load the unavailable videos registry from disk.

    Args:
        data_dir: Data directory (e.g. ~/.kikusan)

    Returns:
        Dict mapping video_id to failure record
    """
    unavailable_file = get_unavailable_file(data_dir)

    if not unavailable_file.exists():
        return {}

    try:
        content = unavailable_file.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict):
            logger.warning("Unavailable file has unexpected format, resetting")
            return {}
        return data
    except json.JSONDecodeError as e:
        logger.error("Corrupted unavailable file: %s", e)
        # Backup corrupted file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = unavailable_file.with_suffix(f".json.corrupt.{timestamp}")
        unavailable_file.rename(backup)
        logger.info("Backed up corrupted unavailable file to: %s", backup)
        return {}
    except Exception as e:
        logger.error("Failed to load unavailable file: %s", e)
        return {}


def save_unavailable(data_dir: Path, data: dict) -> None:
    """Save the unavailable videos registry to disk using atomic write.

    Args:
        data_dir: Data directory (e.g. ~/.kikusan)
        data: Dict mapping video_id to failure record
    """
    unavailable_file = get_unavailable_file(data_dir)
    temp_file = unavailable_file.with_suffix(".json.tmp")

    try:
        json_str = json.dumps(data, indent=2, ensure_ascii=False)
        temp_file.write_text(json_str, encoding="utf-8")
        temp_file.replace(unavailable_file)
        logger.debug("Saved unavailable registry (%d entries)", len(data))
    except Exception as e:
        logger.error("Failed to save unavailable file: %s", e)
        if temp_file.exists():
            temp_file.unlink()
        raise


# --- Fork helpers ----------------------------------------------------------

def _max_attempts() -> int:
    """Failed cycles before giving up (0 = never give up)."""
    try:
        return max(int(os.getenv("KIKUSAN_UNAVAILABLE_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS))), 0)
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS


def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def get_report_file(data_dir: Path) -> Path:
    """Path of videos_no_disponibles.txt (next to the music by default)."""
    override = os.getenv("KIKUSAN_UNAVAILABLE_REPORT_FILE", "").strip()
    if override:
        return Path(override)
    base = data_dir.parent if data_dir.name == ".kikusan" else data_dir
    return base / REPORT_FILENAME


def write_report(data_dir: Path, data: dict | None = None) -> Path | None:
    """Rewrite videos_no_disponibles.txt with every permanently unavailable video.

    Returns the report path, or None if nothing was written.
    """
    if data is None:
        data = load_unavailable(data_dir)

    permanent = sorted(
        ((vid, rec) for vid, rec in data.items() if isinstance(rec, dict) and rec.get("permanent")),
        key=lambda item: item[1].get("permanent_at") or "",
    )
    path = get_report_file(data_dir)
    if not permanent and not path.exists():
        return None

    max_attempts = _max_attempts() or DEFAULT_MAX_ATTEMPTS
    lines = [
        f"# Videos descartados por Kikusan: no estaban disponibles en {max_attempts} ciclos seguidos.",
        "# Cada ciclo es un reintento tras la espera; estos videos ya NO se vuelven a intentar.",
        "# Para reactivar uno, borra su entrada (clave = ID del video) de unavailable.json.",
        f"# Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  Total: {len(permanent)}",
        "#",
        "# ID | Artista - Titulo | URL | Descartado | Intentos | Ultimo error",
    ]
    for vid, rec in permanent:
        artist, title = rec.get("artist"), rec.get("title")
        name = f"{artist} - {title}" if artist and title else (title or artist or "(sin titulo)")
        when = (_parse_timestamp(rec.get("permanent_at")) or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        error = " ".join(str(rec.get("error") or "").split())[:120]
        lines.append(
            f"{vid} | {name} | https://music.youtube.com/watch?v={vid} | {when} | {rec.get('attempts', '?')} | {error}"
        )

    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temp.replace(path)
    except OSError as e:
        logger.error("Could not write unavailable report %s: %s", path, e)
        try:
            temp.unlink()
        except OSError:
            pass
        return None
    return path


def record_unavailable(
    data_dir: Path,
    video_id: str,
    error_message: str,
    title: str | None = None,
    artist: str | None = None,
) -> None:
    """Record a video as unavailable.

    Fork behaviour: counts failed CYCLES. Two records within DEDUP_HOURS count as
    one event (Kikusan reports each failure twice, once with only the ID and once
    with title/artist). After KIKUSAN_UNAVAILABLE_MAX_ATTEMPTS cycles the video is
    marked permanent and listed in videos_no_disponibles.txt.

    Args:
        data_dir: Data directory (e.g. ~/.kikusan)
        video_id: YouTube video ID
        error_message: The error message from yt-dlp
        title: Optional track title for logging
        artist: Optional track artist for logging
    """
    data = load_unavailable(data_dir)
    previous = data.get(video_id)
    previous = previous if isinstance(previous, dict) else {}

    # Already given up on: keep it as is (just fill in missing title/artist)
    if previous.get("permanent"):
        changed = False
        for key, value in (("title", title), ("artist", artist)):
            if value and not previous.get(key):
                previous[key] = value
                changed = True
        if changed:
            data[video_id] = previous
            save_unavailable(data_dir, data)
            write_report(data_dir, data)
        return

    now = datetime.now(timezone.utc)
    previous_at = _parse_timestamp(previous.get("failed_at"))
    previous_attempts = int(previous.get("attempts") or 0)
    age = (now - previous_at) if previous_at else None

    if age is None or age > timedelta(days=STREAK_RESET_DAYS):
        attempts = 1                      # first failure, or the streak was broken long ago
    elif age < timedelta(hours=DEDUP_HOURS) and previous_attempts >= 1:
        attempts = previous_attempts      # same event reported twice
    else:
        attempts = previous_attempts + 1  # a real retry (after the cooldown) failed again

    record = UnavailableRecord(
        failed_at=now.isoformat(),
        error=error_message,
        title=title or previous.get("title"),
        artist=artist or previous.get("artist"),
    )
    entry = record.model_dump()
    entry["attempts"] = attempts
    entry["first_failed_at"] = (
        now.isoformat() if attempts == 1 else previous.get("first_failed_at") or previous.get("failed_at")
    )

    limit = _max_attempts()
    became_permanent = bool(limit) and attempts >= limit
    if became_permanent:
        entry["permanent"] = True
        entry["permanent_at"] = now.isoformat()

    data[video_id] = entry
    save_unavailable(data_dir, data)

    display_name = f"{artist} - {title}" if artist and title else (entry.get("title") or video_id)
    if became_permanent:
        report = write_report(data_dir, data)
        logger.warning(
            "Video permanently marked unavailable after %d failed cycles: %s [%s] (see %s)",
            attempts, display_name, video_id, report,
        )
    else:
        logger.info(
            "Recorded unavailable video: %s (cooldown active, failed cycle %d/%s)",
            display_name, attempts, limit or "unlimited",
        )


def is_on_cooldown(
    data_dir: Path,
    video_id: str,
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
) -> bool:
    """Check if a video ID is on cooldown due to being unavailable.

    Fork behaviour: a video marked permanent is ALWAYS on cooldown.

    Args:
        data_dir: Data directory (e.g. ~/.kikusan)
        video_id: YouTube video ID to check
        cooldown_hours: Hours to wait before retrying (0 = disabled)

    Returns:
        True if the video is still on cooldown
    """
    data = load_unavailable(data_dir)

    if video_id not in data:
        return False

    record = data[video_id]
    if record.get("permanent"):
        return True

    if cooldown_hours <= 0:
        return False

    failed_at_str = record.get("failed_at")

    if not failed_at_str:
        return False

    try:
        failed_at = datetime.fromisoformat(failed_at_str)
        # Ensure timezone-aware comparison
        if failed_at.tzinfo is None:
            failed_at = failed_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        cooldown_end = failed_at + timedelta(hours=cooldown_hours)
        return now < cooldown_end
    except (ValueError, TypeError) as e:
        logger.warning("Invalid timestamp for video %s: %s", video_id, e)
        return False


def get_cooldown_remaining(
    data_dir: Path,
    video_id: str,
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
) -> timedelta | None:
    """Get remaining cooldown time for a video.

    Fork behaviour: permanent videos report a very long remaining time.

    Args:
        data_dir: Data directory (e.g. ~/.kikusan)
        video_id: YouTube video ID
        cooldown_hours: Hours to wait before retrying

    Returns:
        Remaining cooldown timedelta, or None if not on cooldown
    """
    data = load_unavailable(data_dir)

    if video_id not in data:
        return None

    record = data[video_id]
    if record.get("permanent"):
        return PERMANENT_REMAINING

    if cooldown_hours <= 0:
        return None

    failed_at_str = record.get("failed_at")

    if not failed_at_str:
        return None

    try:
        failed_at = datetime.fromisoformat(failed_at_str)
        if failed_at.tzinfo is None:
            failed_at = failed_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        cooldown_end = failed_at + timedelta(hours=cooldown_hours)
        remaining = cooldown_end - now
        if remaining.total_seconds() > 0:
            return remaining
        return None
    except (ValueError, TypeError):
        return None


def clear_expired(
    data_dir: Path,
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
) -> int:
    """Remove stale entries from the unavailable registry.

    Fork behaviour: permanent entries are never removed, and an entry is only
    dropped once its streak could no longer count as "consecutive"
    (older than STREAK_RESET_DAYS), so the failed-cycle counter survives the
    cooldown.

    Args:
        data_dir: Data directory (e.g. ~/.kikusan)
        cooldown_hours: Minimum age in hours before an entry may be removed

    Returns:
        Number of entries removed
    """
    data = load_unavailable(data_dir)

    if not data:
        return 0

    now = datetime.now(timezone.utc)
    keep_for = timedelta(hours=max(cooldown_hours, 0), days=0)
    keep_for = max(keep_for, timedelta(days=STREAK_RESET_DAYS))
    expired_ids = []

    for video_id, record in data.items():
        if isinstance(record, dict) and record.get("permanent"):
            continue
        failed_at_str = record.get("failed_at") if isinstance(record, dict) else None
        if not failed_at_str:
            expired_ids.append(video_id)
            continue

        try:
            failed_at = datetime.fromisoformat(failed_at_str)
            if failed_at.tzinfo is None:
                failed_at = failed_at.replace(tzinfo=timezone.utc)
            if now >= failed_at + keep_for:
                expired_ids.append(video_id)
        except (ValueError, TypeError):
            expired_ids.append(video_id)

    if expired_ids:
        for video_id in expired_ids:
            del data[video_id]
        save_unavailable(data_dir, data)
        logger.info("Cleared %d expired unavailable entries", len(expired_ids))

    return len(expired_ids)
