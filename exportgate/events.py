"""In-memory live feed for the console (server-sent events). Never persisted; the audit log is the record."""
from __future__ import annotations

import threading
import time
from collections import deque

_buf: deque = deque(maxlen=4000)
_lock = threading.Lock()
_seq = 0


def publish(kind: str, **data) -> int:
    global _seq
    with _lock:
        _seq += 1
        _buf.append({"seq": _seq, "t": time.time(), "kind": kind, **data})
        return _seq


def since(seq: int, limit: int = 300) -> list[dict]:
    with _lock:
        return [e for e in _buf if e["seq"] > seq][:limit]


def head() -> int:
    return _seq
