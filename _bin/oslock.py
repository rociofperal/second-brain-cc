"""Cross-platform exclusive file lock: fcntl.flock on POSIX, msvcrt.locking on Windows.

    lock(fh_or_fd, blocking=True)   take an exclusive lock on the whole file
    unlock(fh_or_fd)                release it

Both accept a file object (anything with .fileno()) or an int fd. Stdlib only, 3.9+.

POSIX is exactly fcntl.flock(fd, LOCK_EX[|LOCK_NB]) / flock(fd, LOCK_UN): an advisory lock
that dies with the process. A non-blocking attempt on a held lock raises BlockingIOError
(errno EAGAIN/EWOULDBLOCK), as flock does.

Windows has no flock. msvcrt.locking takes a MANDATORY byte-range lock, so locking the bytes
the file actually holds would make the holder's own reads of other handles, and everybody
else's, fail. We lock one byte at LOCK_OFFSET instead, far past anything a lock file ever
contains: locking beyond end of file is allowed, and nobody reads or writes there, so normal
I/O on the lock file is never blocked. The OS position is saved and restored around the call,
so a buffered file object's view of its position stays correct. LK_LOCK gives up after about
10 s, so blocking mode loops on LK_NBLCK with a short sleep until it gets the lock. A
non-blocking attempt on a held lock raises BlockingIOError(EAGAIN), the same as POSIX, so
callers that catch OSError / check errno behave identically on both.
"""
import errno
import os

try:
    import fcntl
except ImportError:          # Windows
    fcntl = None
    import msvcrt
    import time

# 2**30: well past any lock file's data, and still inside a signed 32-bit offset, which
# is all some C runtimes' _locking accepts.
LOCK_OFFSET = 1 << 30
_POLL = 0.05


def _fd(fh_or_fd):
    return fh_or_fd if isinstance(fh_or_fd, int) else fh_or_fd.fileno()


if fcntl is not None:
    def lock(fh_or_fd, blocking=True):
        """Exclusive lock. Non-blocking and already held: raises BlockingIOError."""
        fcntl.flock(_fd(fh_or_fd), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(fh_or_fd):
        """Release a lock taken with lock()."""
        fcntl.flock(_fd(fh_or_fd), fcntl.LOCK_UN)

else:
    def _locking(fd, mode):
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, mode, 1)
        finally:
            os.lseek(fd, pos, os.SEEK_SET)

    def lock(fh_or_fd, blocking=True):
        """Exclusive lock. Non-blocking and already held: raises BlockingIOError."""
        fd = _fd(fh_or_fd)
        while True:
            try:
                _locking(fd, msvcrt.LK_NBLCK)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                    raise
                if not blocking:
                    raise BlockingIOError(errno.EAGAIN, "lock is held by another handle") from exc
            time.sleep(_POLL)

    def unlock(fh_or_fd):
        """Release a lock taken with lock()."""
        _locking(_fd(fh_or_fd), msvcrt.LK_UNLCK)
