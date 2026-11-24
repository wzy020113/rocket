"""Only reclaim owned, inactive resume files; never follow links or scan payloads."""
import os
from pathlib import Path
import re
import stat
import time

OWNED = re.compile(r'^[0-9a-f]{64}\.(part|tar)$')
MAX_AGE = 7 * 86400


def clean_cache(directory, active=(), now=None):
    directory = Path(directory)
    if directory.name != '.xiaorocket-resume' or directory.is_symlink():
        return 0, 0
    active = set(active)
    now = time.time() if now is None else now
    removed = freed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0, 0
    for path in entries:
        if not OWNED.fullmatch(path.name):
            continue
        # A task locks the .part identity through both receiving and extraction.
        lock = str(path.with_suffix('.part').resolve())
        if lock in active:
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                continue
            if info.st_size and now - info.st_mtime < MAX_AGE:
                continue
            path.unlink()
            removed += 1
            freed += info.st_size
        except OSError:
            continue
    return removed, freed
