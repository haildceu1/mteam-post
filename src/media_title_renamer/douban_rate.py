"""Optional cross-process pacing for authenticated Douban requests."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None


@contextmanager
def _exclusive_lock(handle):
    """Lock the state file across processes on POSIX and Windows."""
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    if msvcrt is None:
        raise OSError("No supported file-locking backend is available")

    # msvcrt.locking locks a byte range beginning at the current file
    # position. Keep byte zero present so the lock works for a fresh file.
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("0")
        handle.flush()

    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError as exc:
            # LK_LOCK stops retrying after a fixed interval. Polling the
            # non-blocking variant lets concurrent CLI processes wait safely.
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            time.sleep(0.05)
    try:
        yield
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def pace_douban_request(url: str) -> None:
    parsed = urlsplit(url)
    search_request = (
        (parsed.hostname == 'search.douban.com' and parsed.path == '/movie/subject_search')
        or (parsed.hostname == 'movie.douban.com' and parsed.path == '/j/subject_suggest')
    )
    if not search_request:
        return
    interval = float(os.environ.get('DOUBAN_REQUEST_INTERVAL', '0'))
    if interval <= 0:
        return
    state = os.environ.get('DOUBAN_RATE_STATE', '')
    if not state:
        raise ValueError('DOUBAN_RATE_STATE is required when pacing is enabled')
    path = Path(state)
    if path.is_relative_to(Path('/tmp')) or path.is_symlink():
        raise ValueError('unsafe Douban rate state path')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        with _exclusive_lock(handle):
            handle.seek(0)
            try:
                previous = float(handle.read().strip() or '0')
            except ValueError:
                previous = 0
            delay = max(0, previous + interval - time.time())
            if delay:
                time.sleep(delay)
            handle.seek(0)
            handle.truncate()
            handle.write(str(time.time()))
            handle.flush()
            os.fsync(handle.fileno())
