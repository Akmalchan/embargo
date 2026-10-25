"""SQLite state + a tamper-evident audit log.

Audit log hash chain:
    h_0 = SHA256("genesis")
    h_n = SHA256(h_{n-1} || canonical_json(event_n))
Change any old row and every hash after it stops matching; verify() names the first bad row.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from .config import SETTINGS

GENESIS = hashlib.sha256(b"genesis").hexdigest()
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
create table if not exists events(
  seq integer primary key, ts text, type text, actor text, ref text, payload text, prev_hash text, hash text);
create table if not exists requests(
  id text primary key, created text, file_name text, file_path text, sha256 text, requester text,
  recipient text, status text, p real, jurisdiction text, card text, approver text, closed_at text, channel text,
  message_ts text);
create table if not exists sweep_runs(id text primary key, started text, finished text, stats text);
create table if not exists sweep_items(
  run_id text, file text, folder text, p real, jurisdiction text, exposure real, unauthorized integer,
  readers integer, unmarked_copy real, detail text, quarantined integer default 0);
create table if not exists read_cache(sha256 text, mode text, reading text, primary key(sha256, mode));
create table if not exists outbox(id integer primary key, created text, kind text, payload text, tries integer default 0,
  last_error text, sent integer default 0);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            SETTINGS.state_dir.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(SETTINGS.db_path, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("pragma journal_mode=wal")
            _conn.executescript(SCHEMA)
        return _conn


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(prev: str, record: dict) -> str:
    return hashlib.sha256((prev + canonical(record)).encode()).hexdigest()


# ------------------------------------------------------------------ audit

def log(type_: str, actor: str = "", ref: str = "", payload: dict | None = None) -> dict:
    with _lock:
        c = conn()
        row = c.execute("select seq, hash from events order by seq desc limit 1").fetchone()
        seq = (row["seq"] + 1) if row else 1
        prev = row["hash"] if row else GENESIS
        rec = {"seq": seq, "ts": now(), "type": type_, "actor": actor, "ref": ref, "payload": payload or {}}
        h = _hash(prev, rec)
        c.execute("insert into events values (?,?,?,?,?,?,?,?)",
                  (seq, rec["ts"], type_, actor, ref, canonical(rec["payload"]), prev, h))
        from . import events
        events.publish("audit", seq=seq, type=type_, actor=actor, ref=ref, hash=h[:16])
        return {**rec, "hash": h, "prev_hash": prev}


def events(limit: int = 200, ref: str | None = None) -> list[dict]:
    q = "select * from events" + (" where ref = ?" if ref else "") + " order by seq desc limit ?"
    args = (ref, limit) if ref else (limit,)
    return [dict(r) | {"payload": json.loads(r["payload"])} for r in conn().execute(q, args).fetchall()]


def verify() -> dict:
    prev = GENESIS
    n = 0
    for r in conn().execute("select * from events order by seq"):
        rec = {"seq": r["seq"], "ts": r["ts"], "type": r["type"], "actor": r["actor"], "ref": r["ref"],
               "payload": json.loads(r["payload"])}
        if r["prev_hash"] != prev or _hash(prev, rec) != r["hash"]:
            return {"ok": False, "checked": n, "broken_at": r["seq"], "type": r["type"],
                    "message": f"chain broken at row {r['seq']}: this row or one before it was changed"}
        prev = r["hash"]
        n += 1
    return {"ok": True, "checked": n, "head": prev, "message": f"all {n} rows intact"}


def tamper(seq: int | None = None) -> dict:
    """Demo only: quietly edit an old decision row the way an insider would."""
    c = conn()
    row = c.execute("select * from events where type = 'DECISION' order by seq limit 1").fetchone() if seq is None \
        else c.execute("select * from events where seq = ?", (seq,)).fetchone()
    if not row:
        return {"ok": False, "message": "no row to edit"}
    p = json.loads(row["payload"])
    p["action"] = "RELEASE"
    p["edited"] = "changed after the fact"
    c.execute("update events set payload = ? where seq = ?", (canonical(p), row["seq"]))
    return {"ok": True, "seq": row["seq"], "message": f"row {row['seq']} edited directly in the database"}


# ------------------------------------------------------------------ requests

def new_request(file_name, file_path, sha, requester, recipient, card, status, p, jurisdiction, channel="") -> str:
    rid = "EG-" + uuid.uuid4().hex[:6].upper()
    conn().execute("insert into requests values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rid, now(), file_name, file_path, sha, requester, recipient, status, p, jurisdiction,
                    canonical(card), None, None, channel, None))
    return rid


def get_request(rid: str) -> dict | None:
    r = conn().execute("select * from requests where id = ?", (rid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["card"] = json.loads(d["card"])
    return d


def list_requests(limit=100) -> list[dict]:
    out = []
    for r in conn().execute("select * from requests order by created desc limit ?", (limit,)):
        d = dict(r)
        d["card"] = json.loads(d["card"])
        out.append(d)
    return out


def update_request(rid: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = [canonical(v) if k == "card" else v for k, v in fields.items()]
    conn().execute(f"update requests set {cols} where id = ?", (*vals, rid))


# ------------------------------------------------------------------ read cache

def cached_reading(sha: str, mode: str):
    r = conn().execute("select reading from read_cache where sha256 = ? and mode = ?", (sha, mode)).fetchone()
    return json.loads(r["reading"]) if r else None


def cache_reading(sha: str, mode: str, reading: dict):
    conn().execute("insert or replace into read_cache values (?,?,?)", (sha, mode, canonical(reading)))


# ------------------------------------------------------------------ outbox (messages that couldn't reach Slack yet)

def outbox_add(kind: str, payload: dict, error: str = "") -> int:
    cur = conn().execute("insert into outbox(created, kind, payload, last_error) values (?,?,?,?)",
                         (now(), kind, canonical(payload), error[:300]))
    return cur.lastrowid


def outbox_pending(limit: int = 50) -> list[dict]:
    rows = conn().execute("select * from outbox where sent = 0 order by id limit ?", (limit,)).fetchall()
    return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]


def outbox_done(oid: int, ok: bool, error: str = ""):
    if ok:
        conn().execute("update outbox set sent = 1 where id = ?", (oid,))
    else:
        conn().execute("update outbox set tries = tries + 1, last_error = ? where id = ?", (error[:300], oid))


def reset(keep_cache=True):
    c = conn()
    for t in ("events", "requests", "sweep_runs", "sweep_items", "outbox") + (() if keep_cache else ("read_cache",)):
        c.execute(f"delete from {t}")


def timer():
    t0 = time.perf_counter()
    return lambda: round((time.perf_counter() - t0) * 1000, 1)
