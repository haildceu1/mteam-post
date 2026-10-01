"""Optional cross-process pacing for authenticated Douban requests."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import time
from urllib.parse import urlsplit


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
        fcntl.flock(handle, fcntl.LOCK_EX)
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
