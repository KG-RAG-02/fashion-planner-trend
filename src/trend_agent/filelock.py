"""프로세스 간 파일 잠금. POSIX는 `fcntl.flock`, Windows는 `msvcrt.locking`을 쓴다.

    with file_lock(path):                   # 잠금이 풀릴 때까지 기다린다
        ...
    with file_lock(path, blocking=False):   # 이미 잠겨 있으면 BlockingIOError

잠금은 `<path>.lock` 옆 파일에 건다. 대상 파일을 연 채로 잠그면 Windows에서는 다른 프로세스의 읽기·쓰기까지 막기 때문이다.
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

__all__ = ["file_lock"]


def _acquire(f, blocking: bool) -> None:
    if sys.platform == "win32":
        while True:
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if not blocking:
                    raise BlockingIOError("이미 잠겨 있다") from None
                time.sleep(0.05)
    else:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


def _release(f) -> None:
    if sys.platform == "win32":
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path | str, *, blocking: bool = True) -> Iterator[None]:
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as f:
        _acquire(f, blocking)
        try:
            yield
        finally:
            _release(f)
