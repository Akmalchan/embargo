"""Kestrel Mail: the fictional company's everyday webmail, a separate site from the Embargo console.

It sends through Embargo exactly like a real company mail system would: over SMTP to the outgoing
mail server (127.0.0.1:2525). When Embargo refuses a message (550), this server does what any mail
server does and puts a bounce in the sender's inbox.

Run:  .venv/bin/uvicorn exportgate.webmail:app --host 127.0.0.1 --port 8791
Open: http://mail.kestrel.localhost:8791   (any *.localhost name points at this machine)
"""
from __future__ import annotations

import json
import mimetypes
import re
import smtplib
import time
import uuid
from email import policy
from email.message import EmailMessage
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from .config import ROOT, SETTINGS
from .registry import registry

app = FastAPI(title="Kestrel Mail")
BOXES = SETTINGS.data_dir / "webmail"
SMTP_PORT = 2525
EMBARGO = "http://127.0.0.1:8790"
ME_DEFAULT = "marco.bellini@kestrelprecision.example"

SEED = [
    ("hans.weber@mueller-praezision.example", "Hans Weber", "RFQ: sensor-mount bracket KP-1023",
     "Hi Marco,\n\nWe can quote the sensor-mount bracket by Friday if you send us the drawing. Views only is fine, "
     "no need for the title block.\n\nBest regards,\nHans Weber\nMueller Praezision GmbH, Stuttgart", 34),
    ("dana.whitfield@kestrelprecision.example", "Dana Whitfield", "Reminder: export-controlled drawings",
     "Team,\n\nReminder that HALCYON and SKYWARD-7 drawings are ITAR-controlled. Don't send them outside the US "
     "without a license. Embargo checks every attachment, so if something is held, ping me.\n\nDana\nEmpowered Official", 140),
    ("buyer@northfield-avionics.example", "R. Castillo", "PO 4471: revised bracket drawing",
     "Marco, can you resend the latest rev of the bracket drawing for PO 4471? Thanks, R. Castillo, Northfield Avionics", 260),
    ("sales@tritonmarine.example", "B. Olsen", "Quote for catalog part KP-1047",
     "Hello, could you send the drawing for catalog part KP-1047 so we can confirm fit? Regards, B. Olsen, Triton Marine", 520),
    ("jamie.ortega@kestrelprecision.example", "Jamie Ortega", "Shop schedule this week",
     "5-axis cell is down Tuesday for maintenance. Travelers due Wednesday. -J", 900),
]


def _box(user: str, folder: str) -> Path:
    p = BOXES / re.sub(r"[^a-z0-9@._-]", "_", user.lower()) / folder
    p.mkdir(parents=True, exist_ok=True)
    return p


def _put(user, folder, msg: dict) -> dict:
    msg.setdefault("id", uuid.uuid4().hex[:10])
    msg.setdefault("ts", time.time())
    (_box(user, folder) / f"{msg['id']}.json").write_text(json.dumps(msg))
    return msg


def _seed(user: str):
    inbox = _box(user, "inbox")
    if any(inbox.glob("*.json")):
        return
    now = time.time()
    for frm, name, subj, body, mins in SEED:
        if frm == user:
            continue
        _put(user, "inbox", {"from": frm, "from_name": name, "to": [user], "subject": subj, "body": body,
                             "ts": now - mins * 60, "unread": mins < 200, "attachments": []})


def _list(user, folder):
    _seed(user)
    msgs = [json.loads(p.read_text()) for p in _box(user, folder).glob("*.json")]
    return sorted(msgs, key=lambda m: -m["ts"])


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((ROOT / "web" / "mail" / "index.html").read_text(), headers={"Cache-Control": "no-cache"})


def _safe(s: str) -> str:  # the same file-name cleaning the outgoing mail server applies
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", s)[:80] or "x"


def _render_png(data: bytes, name: str, w: int) -> bytes:
    """A colour preview of page 1 (PDF) or the image itself, w pixels wide."""
    import cv2
    import numpy as np
    w = max(60, min(w, 1400))
    if Path(name).suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            page = doc[0]
            z = w / max(page.rect.width, 1)
            return page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False).tobytes("png")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:   # Word / PowerPoint / ZIP / CNC program: preview the first page Embargo will look at
        import tempfile
        from .ingest import SUPPORTED_EXT, load
        if Path(name).suffix.lower() not in SUPPORTED_EXT:
            raise HTTPException(415, "not a picture")
        with tempfile.NamedTemporaryFile(suffix=Path(name).suffix) as tf:
            tf.write(data); tf.flush()
            img = cv2.cvtColor(load(tf.name, dpi=60).pages[0].image, cv2.COLOR_GRAY2BGR)
    s = w / img.shape[1]
    img = cv2.resize(img, (w, max(1, int(img.shape[0] * s))), interpolation=cv2.INTER_AREA)
    return cv2.imencode(".png", img)[1].tobytes()


@app.get("/api/contacts")
def contacts():
    reg = registry()
    out = [{"email": p.email, "name": p.name, "org": "Kestrel Precision", "external": False} for p in reg.staff.values()]
    out += [{"email": r.email, "name": r.name, "org": f"{r.company} · {r.country}", "external": True} for r in reg.recipients.values()]
    return out


@app.get("/api/folder/{folder}")
def folder(folder: str, me: str = ME_DEFAULT):
    if folder not in ("inbox", "sent", "held"):
        raise HTTPException(404)
    return _list(me, folder)


@app.post("/api/read/{mid}")
def mark_read(mid: str, me: str = ME_DEFAULT):
    p = _box(me, "inbox") / f"{mid}.json"
    if p.exists():
        m = json.loads(p.read_text()); m["unread"] = False; p.write_text(json.dumps(m))
    return {"ok": True}


@app.get("/api/files")
def files():
    """The built-in demo drawings (hidden picker, Ctrl+Shift+D in the compose window)."""
    import csv
    rows = list(csv.DictReader(open(SETTINGS.data_dir / "outgoing" / "answer_key.csv")))
    return [{"path": r["file"], "name": Path(r["file"]).name, "note": r["variant"].replace("_", " "), "set": r["set"]} for r in rows]


def _data_file(path: str) -> Path:
    p = (SETTINGS.data_dir / path).resolve()
    if SETTINGS.data_dir.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "not found")
    return p


@app.get("/api/demo-file")
def demo_file(path: str):
    """Raw bytes of a demo file, so the hidden picker attaches it exactly like a file from disk."""
    return FileResponse(_data_file(path))


@app.get("/api/thumb")
def thumb(path: str = "", rid: str = "", w: int = 360):
    """Embargo's preview of a demo file (path) or of whatever it checked for a request (rid)."""
    url = f"{EMBARGO}/api/requests/{rid}/thumb" if rid else f"{EMBARGO}/api/thumb"
    try:
        r = httpx.get(url, params={"w": w} if rid else {"path": path, "w": w}, timeout=30)
    except httpx.HTTPError:
        raise HTTPException(503)
    return Response(r.content, media_type="image/png", status_code=r.status_code)


@app.post("/api/preview")
async def preview(file: UploadFile = File(...), w: int = 320):
    """Thumbnail for an attachment chip while composing (what any webmail does after upload)."""
    try:
        png = _render_png(await file.read(), file.filename or "", w)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(415, "no preview")
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


def _att_path(me: str, mid: str, i: int) -> tuple[Path, dict]:
    for f in ("sent", "held", "inbox"):
        p = _box(me, f) / f"{mid}.json"
        if p.exists():
            atts = json.loads(p.read_text()).get("attachments") or []
            if 0 <= i < len(atts) and isinstance(atts[i], dict) and atts[i].get("file"):
                fp = BOXES / atts[i]["file"]
                if fp.exists():
                    return fp, atts[i]
    raise HTTPException(404)


@app.get("/api/att/{mid}/{i}")
def att(mid: str, i: int, me: str = ME_DEFAULT):
    fp, a = _att_path(me, mid, i)
    return FileResponse(fp, filename=a["name"], content_disposition_type="inline")


@app.get("/api/att/{mid}/{i}/thumb")
def att_thumb(mid: str, i: int, w: int = 360, me: str = ME_DEFAULT):
    fp, a = _att_path(me, mid, i)
    cache = fp.parent / f".thumb_{i}_{w}.png"
    if not cache.exists():
        try:
            cache.write_bytes(_render_png(fp.read_bytes(), a["name"], w))
        except Exception:
            raise HTTPException(415)
    return Response(cache.read_bytes(), media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.post("/api/send")
async def send(to: str = Form(...), subject: str = Form(""), body: str = Form(""), me: str = Form(ME_DEFAULT),
               attach_path: list[str] = Form([]), upload: list[UploadFile] = File([])):
    reg = registry()
    rcpts = [t for t in re.split(r"[,;\s]+", to.strip()) if t]
    if not rcpts:
        raise HTTPException(400, "no recipient")
    msg = EmailMessage(policy=policy.SMTP)
    sender = reg.person(me)
    msg["From"] = f"{sender.name if sender else me} <{me}>"
    msg["To"], msg["Subject"] = ", ".join(rcpts), subject or "(no subject)"
    msg.set_content(body or " ")

    parts: list[tuple[str, bytes]] = []  # (file name as the user sees it, bytes)
    for ap in attach_path:
        if ap:
            p = _data_file(ap)
            parts.append((p.name, p.read_bytes()))
    for u in upload or []:
        if u is not None and u.filename:
            parts.append((Path(u.filename).name, await u.read()))
    for name, data in parts:
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

    t0 = time.perf_counter()
    try:
        with smtplib.SMTP("127.0.0.1", SMTP_PORT, timeout=300) as s:
            s.ehlo("mail.kestrel.localhost")
            s.mail(me)
            for r in rcpts:
                s.rcpt(r)
            code, resp = s.data(msg.as_bytes())  # raises SMTPDataError on 5xx/4xx
            reply = resp.decode(errors="replace")
    except smtplib.SMTPDataError as e:
        code, reply = e.smtp_code, e.smtp_error.decode(errors="replace")
    except (smtplib.SMTPException, OSError) as e:
        raise HTTPException(503, f"outgoing mail server not reachable: {e}")
    ms = round((time.perf_counter() - t0) * 1000)
    if code >= 400 and code != 550:  # 451 etc.: the server could not check it, nothing was sent
        raise HTTPException(503, f"outgoing mail server: {code} {reply}")

    # ask Embargo what it decided for each attachment (its newest requests are this message's checks)
    ids = re.findall(r"EG-[0-9A-F]{6}", reply)
    cards: dict[str, dict] = {}
    per_att: list[str] = []
    n = len(parts) * len(rcpts)
    try:
        if n:
            recent = httpx.get(f"{EMBARGO}/api/requests", params={"limit": n + 8}, timeout=10).json()
            mine = [r for r in recent if r.get("requester") == me.lower() and r.get("recipient") in {x.lower() for x in rcpts}][:n]
            # match each attachment to its check by the file name the mail server saved it under
            pool = [(re.sub(r"^mail_[0-9a-f]{6}_", "", r.get("file", "")), r["id"]) for r in reversed(mine)]
            for name, _ in parts:
                hit = next((x for x in pool if x[0] == _safe(name)), None)
                per_att.append(hit[1] if hit else "")
                if hit:
                    pool.remove(hit)
            for rid in dict.fromkeys(x for x in ids + per_att if x):
                cards[rid] = httpx.get(f"{EMBARGO}/api/requests/{rid}", timeout=10).json().get("card") or {}
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    if not ids and per_att:
        ids = per_att[:1]
    lead = ids[0] if ids else ""
    card = cards.get(lead)
    action = (card or {}).get("action") or ("BLOCK" if code == 550 else "RELEASE")
    outcome = "blocked" if code == 550 else "held" if action == "HOLD" else "sent"

    mid = uuid.uuid4().hex[:10]
    files_dir = _box(me, "files") / mid
    files_dir.mkdir(parents=True, exist_ok=True)
    att_meta = []
    for i, (name, data) in enumerate(parts):
        (files_dir / f"{i}_{_safe(name)}").write_bytes(data)
        rid = per_att[i] if i < len(per_att) else (lead if len(parts) == 1 else "")
        att_meta.append({"name": name, "size": len(data), "file": str((files_dir / f"{i}_{_safe(name)}").relative_to(BOXES)),
                         "embargo_id": rid, "action": (cards.get(rid) or {}).get("action", "")})
    lead_idx = next((i for i, a in enumerate(att_meta) if a["embargo_id"] == lead), 0)

    sent = _put(me, "held" if outcome == "held" else "sent", {
        "id": mid, "from": me, "from_name": sender.name if sender else me, "to": rcpts, "subject": msg["Subject"], "body": body,
        "attachments": att_meta, "outcome": outcome, "embargo_id": lead, "unread": False})
    if outcome == "blocked":  # what any mail server does with a 550: bounce it back to the sender
        reason = re.sub(r"^(?:550 )?\d\.\d\.\d+ ", "", reply)
        for name, _ in parts:  # the server reports the cleaned file name; show the one the user attached
            reason = reason.replace(_safe(name), name)
        rc = (card or {}).get("recipient") or {}
        _put(me, "inbox", {
            "from": "mailer-daemon@kestrelprecision.example", "from_name": "Mail Delivery System",
            "to": [me], "subject": f"Undeliverable: {msg['Subject']}", "unread": True, "attachments": [], "bounce": True,
            "embargo_id": lead,
            "ndr": {"to": rcpts, "to_name": rc.get("name", ""), "company": rc.get("company", ""), "country": rc.get("country", ""),
                    "code": code, "status": "5.7.1", "reason": reason, "reply": reply, "orig_subject": msg["Subject"],
                    "orig_id": mid, "orig_ts": sent["ts"], "attachments": [a["name"] for a in att_meta],
                    "blocked_file": att_meta[lead_idx]["name"] if att_meta else ""},
            "body": f"Your message to {', '.join(rcpts)} was not delivered.\n\nThe outgoing mail server (Embargo) refused it:\n\n"
                    f"    {code} {reason}\n\nNothing left the building. If you think this is wrong, ask an Empowered Official "
                    f"(Dana Whitfield or Lee Okafor) to review request {lead}."})
    return {"outcome": outcome, "code": code, "reply": reply, "ids": ids, "ms": ms, "card": card,
            "attachments": att_meta, "lead": lead_idx, "sent_id": sent["id"],
            "checks": [{"name": a["name"], "id": a["embargo_id"], "action": a["action"]} for a in att_meta]}
