from __future__ import annotations

from pathlib import Path

import pytest

from dvoretskyi.agent import photo_store
from dvoretskyi.config import get_settings


def _make_image(path: Path, size: tuple[int, int] = (3000, 2000)) -> None:
    from PIL import Image

    Image.new("RGB", size, (120, 130, 140)).save(path, format="JPEG", quality=95)


@pytest.fixture
def _archive_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "meter_photo_dir", str(tmp_path / "archive"))
    return tmp_path


def test_archive_compresses_and_downscales(_archive_dir, monkeypatch):
    from PIL import Image

    src = _archive_dir / "big.jpg"
    _make_image(src, (3000, 2000))
    out = photo_store.archive(str(src), reading_id=42)
    assert out is not None
    archived = Path(out)
    assert archived.is_file() and archived.name == "meter_42.jpg"
    # Downscaled to the configured long side, and smaller on disk.
    with Image.open(archived) as img:
        assert max(img.size) <= get_settings().meter_photo_max_long_side
    assert archived.stat().st_size < src.stat().st_size


def test_exists_and_remove_guarded_to_archive(_archive_dir, monkeypatch):
    src = _archive_dir / "x.jpg"
    _make_image(src, (400, 300))
    out = photo_store.archive(str(src), reading_id=7)
    assert photo_store.exists(out) is True

    # remove() only deletes inside the archive dir — never an arbitrary path.
    photo_store.remove(str(src))  # outside archive → left alone
    assert src.is_file()
    photo_store.remove(out)  # inside archive → gone
    assert photo_store.exists(out) is False


def test_archive_bad_input_returns_none(_archive_dir):
    assert photo_store.archive("/no/such/file.jpg", reading_id=1) is None
    assert photo_store.exists(None) is False
    photo_store.remove(None)  # no-op, no raise
