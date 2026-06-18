"""Persistent, compressed archive for meter photos (L2).

A meter photo the user sends is OCR'd from a *temp* download that's deleted right after.
But the user also wants to pull a meter's photo back later («витягни фото газу»), so we
keep ONE compressed JPEG per reading here — small (downscaled + re-encoded), private
(0o700 dir), and never logged. The archive path is stored in `MeterReading.photo_ref`.

Lifecycle mirrors the readings:
- a fresh draft supersedes the old one → the old archived photo is removed (`remove`);
- a wrong reading deleted by the user → its photo is removed too;
- a *submitted* reading is the permanent record → its photo is kept.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dvoretskyi.config import get_settings

log = logging.getLogger(__name__)


def photo_dir() -> Path:
    """The private archive directory (created on demand, owner-only)."""
    raw = get_settings().meter_photo_dir.strip()
    path = Path(raw).expanduser() if raw else Path.home() / ".dvoretskyi" / "meter_photos"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _archive_path(reading_id: int) -> Path:
    return photo_dir() / f"meter_{reading_id}.jpg"


def archive(src_path: str, reading_id: int) -> str | None:
    """Compress `src_path` into the archive as `meter_<id>.jpg`; return its path.

    Downscales to `meter_photo_max_long_side` and re-encodes JPEG at
    `meter_photo_quality` so a phone photo shrinks from MBs to tens of KB. On any Pillow
    error returns None (the caller keeps whatever ref it had — archiving is best-effort).
    """
    st = get_settings()
    try:
        from PIL import Image

        dest = _archive_path(reading_id)
        with Image.open(src_path) as img:
            rgb = img.convert("RGB")
            rgb.thumbnail((st.meter_photo_max_long_side, st.meter_photo_max_long_side))
            rgb.save(
                dest,
                format="JPEG",
                quality=st.meter_photo_quality,
                optimize=True,
                progressive=True,
            )
        return str(dest)
    except Exception as exc:  # noqa: BLE001 — never block the reading on an archive miss
        log.warning("meter photo archive failed (%s)", exc)  # bytes/path-only, no PII
        return None


def exists(photo_ref: str | None) -> bool:
    """True if the archived photo file is still on disk."""
    if not photo_ref:
        return False
    try:
        return Path(photo_ref).is_file()
    except OSError:
        return False


def remove(photo_ref: str | None) -> None:
    """Delete an archived photo when its reading is superseded/deleted.

    Guarded to the archive dir so we never unlink an arbitrary path (e.g. a temp download
    still referenced elsewhere). Silent if the file is already gone.
    """
    if not photo_ref:
        return
    try:
        ref = Path(photo_ref).resolve()
        if ref.parent == photo_dir().resolve() and ref.exists():
            ref.unlink()
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        log.warning("meter photo remove failed (%s)", exc)
