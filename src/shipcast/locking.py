"""Project-level concurrency control via advisory `fcntl.flock`.

Only one shipcast process at a time may hold a project's lock. The dispatcher
acquires it once per CLI invocation that mutates manifest state. Read-only
commands (`shipcast status`) do NOT acquire — the manifest's atomic-write
contract guarantees consistent reads.

Lifecycle (clean exit):
1. Acquire flock (LOCK_EX | LOCK_NB; raises ProjectLocked if held elsewhere).
2. Write own pid to the lock file (diagnostic).
3. Yield.
4. Release flock.
5. Unlink the lock file (best-effort).
6. Close the fd.

Order matters: release the flock BEFORE unlinking and BEFORE closing the fd.
A close-before-unlink path is harmless but unnecessary; an unlink-before-flock-
release would leave a small window where a competing process opens the path,
creates a new inode, and flocks it — both processes would then hold valid
flocks on different inodes. The release-first order is the correct POSIX
recipe.

On crash exit the kernel releases the flock automatically; the file may
persist on disk and is .gitignored.
"""

from __future__ import annotations

import fcntl
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from shipcast.errors import ProjectLocked, UnsupportedPlatform

#: Platforms where `fcntl.flock` semantics match our design.
SUPPORTED_PLATFORMS: Final[frozenset[str]] = frozenset({"darwin", "linux"})


def check_platform() -> None:
    """Refuse to run on any non-macOS/Linux platform. Call once at CLI startup."""
    if sys.platform not in SUPPORTED_PLATFORMS:
        raise UnsupportedPlatform(
            f"shipcast is supported on macOS and Linux only; got sys.platform={sys.platform!r}"
        )


@contextmanager
def acquire(lock_path: Path) -> Iterator[None]:
    """Acquire an exclusive advisory lock on `lock_path` for the duration of the context.

    The file is created if missing. The current pid is written to it for
    diagnostic purposes (`cat <project>/.lock` shows who holds it).

    Raises:
        ProjectLocked: another process already holds the flock.
        UnsupportedPlatform: `sys.platform` is not in SUPPORTED_PLATFORMS.
    """
    check_platform()

    # Touch the file (creating if missing), then open RW for flock.
    lock_path.touch(exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProjectLocked(f"another shipcast process holds {lock_path}") from exc

        # Write our pid for diagnostic visibility.
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            pass  # diagnostic write is best-effort

        yield
    finally:
        # Release flock BEFORE unlinking — see module docstring.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
