"""Embargo as the company's outgoing mail server: real SMTP on 127.0.0.1:2525.

Point any mail client (or curl, or Python's smtplib) at it. For each attachment and recipient:
  BLOCK   -> the whole message is refused:  550 5.7.1 Blocked by Embargo: <why> [EG-…]
  HOLD    -> accepted and quarantined:      250 … held for sign-off [EG-…]; delivered when an
             Empowered Official approves it, dropped if they deny it
  RELEASE -> delivered to the recipient's mailbox (data/mail/delivered/<recipient>/)
This is how corporate mail filters behave; here the filter is the export-control engine.

  EG_SMTP=0  turn it off      EG_SMTP_PORT=2525
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from email import policy
from email.parser import BytesParser
from pathlib import Path

from . import events, store
from .config import SETTINGS
from .engine import check_outgoing
from .ingest import SUPPORTED_EXT

MAIL = SETTINGS.data_dir / "mail"
PORT = int(os.environ.get("EG_SMTP_PORT", "2525"))
_controller = None


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", s)[:80] or "x"


def _deliver(raw: bytes, rcpt: str, subject: str) -> Path:
    box = MAIL / "delivered" / _safe(rcpt.lower())
    box.mkdir(parents=True, exist_ok=True)
    p = box / f"{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:4]}_{_safe(subject)}.eml"
    p.write_bytes(raw)
    return p


def process(mail_from: str, rcpts: list[str], raw: bytes) -> str:
    """Check every attachment for every recipient. Returns the SMTP reply line."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(msg.get("Subject", "(no subject)"))
    saved: list[Path] = []
    for part in msg.iter_attachments():
        name = Path(part.get_filename() or "attachment.bin").name
        if Path(name).suffix.lower() not in SUPPORTED_EXT:
            continue
        dst = SETTINGS.state_dir / "inbox" / f"mail_{uuid.uuid4().hex[:6]}_{_safe(name)}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(part.get_content() if isinstance(part.get_content(), bytes) else part.get_payload(decode=True))
        saved.append(dst)

    cards = [check_outgoing(p, mail_from, r, channel="email") for r in rcpts for p in saved]
    ids = [c["id"] for c in cards]
    base = {"from": mail_from, "to": rcpts, "subject": subject, "attachments": [p.name.split("_", 2)[-1] for p in saved], "ids": ids}

    def lead(c):
        return next((r for k, r in zip(c["codes"], c["reasons"]) if k.startswith(c["action"])), c["reasons"][0] if c["reasons"] else "")

    blocked = [c for c in cards if c["action"] == "BLOCK"]
    if blocked:
        c = blocked[0]
        reply = f"550 5.7.1 Blocked by Embargo: {c['file'].split('_', 2)[-1]} to {c['recipient']['company']} ({c['recipient']['country']}): {lead(c)} [{c['id']}]"
        store.log("MAIL_REFUSED", mail_from, c["id"], base | {"reply": reply})
        events.publish("mail", result="refused", reply=reply, **base)
        return reply
    held = [c for c in cards if c["action"] == "HOLD"]
    if held:
        (MAIL / "held").mkdir(parents=True, exist_ok=True)
        for c in held:
            (MAIL / "held" / f"{c['id']}.eml").write_bytes(raw)
            (MAIL / "held" / f"{c['id']}.json").write_text(json.dumps({"rcpts": rcpts, "subject": subject, "from": mail_from}))
        reply = f"250 2.0.0 Accepted, held by Embargo until an Empowered Official signs off [{', '.join(c['id'] for c in held)}]"
        store.log("MAIL_HELD", mail_from, held[0]["id"], base | {"reply": reply})
        events.publish("mail", result="held", reply=reply, **base)
        return reply
    for r in rcpts:
        _deliver(raw, r, subject)
    reply = f"250 2.0.0 Checked by Embargo and delivered{' [' + ', '.join(ids) + ']' if ids else ' (no attachments)'}"
    store.log("MAIL_DELIVERED", mail_from, ids[0] if ids else "", base | {"reply": reply})
    events.publish("mail", result="delivered", reply=reply, **base)
    return reply


def release_held(rid: str, approved: bool) -> bool:
    """Called when an Empowered Official decides on a held request."""
    eml, meta = MAIL / "held" / f"{rid}.eml", MAIL / "held" / f"{rid}.json"
    if not eml.exists():
        return False
    info = json.loads(meta.read_text()) if meta.exists() else {"rcpts": [], "subject": ""}
    if approved:
        for r in info["rcpts"]:
            _deliver(eml.read_bytes(), r, info.get("subject", ""))
        store.log("MAIL_DELIVERED", "embargo", rid, {"after": "sign-off", "to": info["rcpts"]})
        events.publish("mail", result="delivered", reply=f"250 delivered after sign-off [{rid}]", ids=[rid], to=info["rcpts"],
                       subject=info.get("subject", ""), attachments=[], **{"from": info.get("from", "")})
    dest = MAIL / "held" / ("released" if approved else "denied")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(eml), dest / eml.name)
    if meta.exists():
        shutil.move(str(meta), dest / meta.name)
    return True


def mailboxes() -> dict:
    out = {}
    for box in sorted((MAIL / "delivered").glob("*")) if (MAIL / "delivered").exists() else []:
        msgs = []
        for f in sorted(box.glob("*.eml"), reverse=True)[:20]:
            m = BytesParser(policy=policy.default).parsebytes(f.read_bytes())
            msgs.append({"subject": str(m.get("Subject", "")), "from": str(m.get("From", "")), "date": f.name[:15],
                         "attachments": [p.get_filename() for p in m.iter_attachments()]})
        out[box.name] = msgs
    held = sorted(p.stem for p in (MAIL / "held").glob("*.eml")) if (MAIL / "held").exists() else []
    return {"delivered": out, "held": held, "port": PORT}


class _Handler:
    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, process, envelope.mail_from, list(envelope.rcpt_tos), envelope.content)
        except Exception as e:  # never 500 silently
            return f"451 4.3.0 Embargo could not check this message: {str(e)[:120]}"


def stop():
    global _controller
    if _controller:
        try:
            _controller.stop()
        finally:
            _controller = None


def start():
    """Start in the background, retrying while a previous run still holds the port."""
    if _controller or os.environ.get("EG_SMTP", "1") == "0":
        return
    import threading

    def run():
        global _controller
        from aiosmtpd.controller import Controller
        MAIL.mkdir(parents=True, exist_ok=True)
        last = ""
        for _ in range(30):
            c = Controller(_Handler(), hostname="127.0.0.1", port=PORT, server_hostname="embargo.kestrelprecision.local")
            try:
                c.start()
                _controller = c
                return
            except Exception as e:  # port busy etc.
                last = str(e)[:200]
                time.sleep(1)
        store.log("SMTP_START_FAILED", "mailgate", "", {"error": last})
    threading.Thread(target=run, daemon=True, name="smtp-start").start()
