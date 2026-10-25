"""Always-on outgoing folder: drop a file in data/drop/<recipient email>/ and it is checked.

Stands in for a mail or upload gateway, and works with no network at all. Checked files move to
data/drop/_done/ (or _failed/), and the decision lands in the log, the console and, when Slack is
reachable, the channel.

  EG_WATCH=0               turn it off
  EG_DROP_REQUESTER=...    whose request a dropped file counts as (default: an engineer)
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from . import events, store
from .config import SETTINGS
from .engine import check_outgoing
from .ingest import SUPPORTED_EXT

DROP = SETTINGS.data_dir / "drop"
REQUESTER = os.environ.get("EG_DROP_REQUESTER", "marco.bellini@kestrelprecision.example")
_started = False


def _ready(p: Path) -> bool:
    """Skip files still being copied in: size unchanged for half a second."""
    try:
        a = p.stat().st_size
        time.sleep(0.5)
        return a == p.stat().st_size and a > 0
    except FileNotFoundError:
        return False


def scan_once() -> int:
    n = 0
    for d in sorted(DROP.iterdir()) if DROP.exists() else []:
        if not d.is_dir() or d.name.startswith("_") or "@" not in d.name:
            continue
        for f in sorted(d.iterdir()):
            if not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in SUPPORTED_EXT:
                continue
            if not _ready(f):
                continue
            try:
                card = check_outgoing(f, REQUESTER, d.name, channel="drop")
                dest = DROP / "_done" / f"{card['id']}_{f.name}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), dest)
                store.update_request(card["id"], file_path=str(dest))
                events.publish("drop", id=card["id"], file=f.name, recipient=d.name, action=card["action"])
            except Exception as e:  # never stop watching
                bad = DROP / "_failed" / f.name
                bad.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), bad)
                store.log("DROP_FAILED", "watcher", "", {"file": f.name, "error": str(e)[:200]})
            n += 1
    return n


def loop(interval: float = 2.0):
    while True:
        try:
            scan_once()
        except Exception:
            pass
        time.sleep(interval)


def start():
    global _started
    if _started or os.environ.get("EG_WATCH", "1") == "0":
        return
    _started = True
    DROP.mkdir(parents=True, exist_ok=True)
    (DROP / "README.txt").write_text(
        "Make a folder named after the recipient's email, e.g.\n"
        "  hans.weber@mueller-praezision.example/\n"
        "and drop the file inside. Embargo checks it within a few seconds.\n")
    threading.Thread(target=loop, daemon=True, name="drop-watcher").start()
