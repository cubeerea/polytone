"""File-permission hygiene: ``data/`` is 0700 and the files in it 0600. See CONTRACTS.md.

The server also sets ``umask(0o077)`` at start, so files it creates later (SQLite ``-wal``/
``-shm``, temp files) are private from birth; ``harden`` fixes what already exists (files made
by older builds, ``backend.log`` created by the Rust shell, a token written by the OAuth flow).
"""
from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)


def _chmod(path: Path, mode: int) -> bool:
    try:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
            return False
        if stat.S_IMODE(st.st_mode) != mode:
            os.chmod(path, mode)
            return True
    except OSError as e:
        log.debug("chmod %s failed: %s", path.name, e)
    return False


def harden(data_dir: str | Path, extra_files: Iterable[str | Path] = ()) -> int:
    """chmod ``data_dir`` 0700, every regular file directly in it 0600 (plus ``extra_files``).
    Subdirectories (e.g. the drop-folder inbox) become 0700; their contents are left alone.
    Returns how many modes changed."""
    d = Path(data_dir)
    changed = 0
    if d.is_dir():
        changed += _chmod(d, 0o700)
        for p in d.iterdir():
            try:
                st = p.lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                changed += _chmod(p, 0o600)
            elif stat.S_ISDIR(st.st_mode):
                changed += _chmod(p, 0o700)
    for f in extra_files:
        p = Path(f)
        if p.is_file():
            changed += _chmod(p, 0o600)
    return changed
