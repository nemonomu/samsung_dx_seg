"""Bounded retention for timestamped MediaMarkt listing HTML archives."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


LISTING_ARCHIVE_RETENTION_HOURS = 48
_ARCHIVE_NAME = re.compile(r"^(main|bsr)_(\d{8}_\d{6})$")


@dataclass(frozen=True)
class CleanupResult:
    deleted: tuple[Path, ...]
    skipped_recently_modified: tuple[Path, ...]
    errors: tuple[str, ...]


def cleanup_listing_archives(
    listing_root: Path,
    *,
    now: datetime | None = None,
    retention_hours: int = LISTING_ARCHIVE_RETENTION_HOURS,
) -> CleanupResult:
    """Delete only inactive main_/bsr_ timestamp directories older than retention."""
    if retention_hours <= 0:
        raise ValueError("retention_hours must be greater than zero")
    if not listing_root.exists():
        return CleanupResult((), (), ())

    current_time = now or datetime.now()
    cutoff = current_time - timedelta(hours=retention_hours)
    deleted: list[Path] = []
    skipped_recently_modified: list[Path] = []
    errors: list[str] = []
    try:
        root_resolved = listing_root.resolve()
        candidates = tuple(listing_root.iterdir())
    except OSError as exc:
        return CleanupResult((), (), (f"listing root: {exc}",))

    for candidate in candidates:
        match = _ARCHIVE_NAME.fullmatch(candidate.name)
        if match is None:
            continue

        try:
            is_junction = getattr(candidate, "is_junction", lambda: False)()
            if candidate.is_symlink() or is_junction or not candidate.is_dir():
                continue
            archive_time = datetime.strptime(match.group(2), "%Y%m%d_%H%M%S")
            if archive_time >= cutoff:
                continue

            # A direct-child/resolve check prevents deletion through redirected paths.
            if candidate.resolve().parent != root_resolved:
                continue

            # Preserve an old-named directory if a crawler is still writing to it.
            modified_time = datetime.fromtimestamp(candidate.stat().st_mtime)
            if modified_time >= cutoff:
                skipped_recently_modified.append(candidate)
                continue

            shutil.rmtree(candidate)
            deleted.append(candidate)
        except (OSError, ValueError) as exc:
            errors.append(f"{candidate.name}: {exc}")

    return CleanupResult(tuple(deleted), tuple(skipped_recently_modified), tuple(errors))
