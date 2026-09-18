"""Small cross-platform primitives for locks and bounded pipe reads."""
import os
import queue
import threading
import time


def lock_file(handle, blocking=True):
    if os.name != 'nt':
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    import msvcrt
    handle.seek(0, 2)
    if not handle.tell():
        handle.write(' ')
        handle.flush()
    deadline = time.monotonic() + (120 if blocking else 0)
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)


class TimedPipe:
    """Windows select cannot watch pipes; use a bounded two-chunk queue."""
    def __init__(self, stream, timeout=60):
        self.stream, self.timeout = stream, timeout
        self.queue = queue.Queue(maxsize=2)
        self.stopped = threading.Event()
        self.buffer = b''
        self.eof = False
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        try:
            while not self.stopped.is_set():
                data = os.read(self.stream.fileno(), 256 * 1024)
                self._put(data)
                if not data:
                    return
        except (OSError, ValueError) as exc:
            self._put(exc)

    def _put(self, item):
        while not self.stopped.is_set():
            try:
                self.queue.put(item, timeout=.2)
                return
            except queue.Full:
                pass

    def read(self, length):
        if not self.buffer and not self.eof:
            try:
                item = self.queue.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise OSError('Media read timed out') from exc
            if isinstance(item, Exception):
                raise OSError('Media pipe failed') from item
            self.buffer = item
            self.eof = not item
        result, self.buffer = self.buffer[:length], self.buffer[length:]
        return result

    def close(self):
        self.stopped.set()
        self.thread.join(1)
