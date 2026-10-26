"""Dashboard + HTTP API (also the tool surface for the OpenClaw agent).

Run:  .venv/bin/uvicorn exportgate.server:app --host 127.0.0.1 --port 8790
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import engine, events, llm, mailgate, perimeter, store, sweep, system, watch
from .config import ROOT, SETTINGS
from .registry import registry

app = FastAPI(title="Embargo", version="0.1")
INBOX = SETTINGS.state_dir / "inbox"
INBOX.mkdir(parents=True, exist_ok=True)


app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")
THUMBS = SETTINGS.state_dir / "thumbs"
THUMBS.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
def _startup():
    watch.start()     # always-on outgoing folder (data/drop/<recipient>/)
    mailgate.start()  # outgoing mail server, SMTP on 127.0.0.1:2525


@app.on_event("shutdown")
def _shutdown():
    mailgate.stop()   # otherwise its thread keeps the old code answering on :2525 after a restart


@app.middleware("http")
async def no_stale_assets(request, call_next):
    """Browsers (Safari especially) cache CSS/JS hard; always revalidate so a new build shows up."""
    resp = await call_next(request)
    if request.url.path.startswith("/static/") and request.url.path.endswith((".css", ".js")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _versioned(html: str) -> str:
    for name in ("console.css", "console.js"):
        v = int((ROOT / "web" / "static" / name).stat().st_mtime)
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={v}\"")
    return html


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_versioned((ROOT / "web" / "console.html").read_text()), headers={"Cache-Control": "no-cache"})


@app.get("/classic", response_class=HTMLResponse)
def classic():
    return (ROOT / "web" / "index.html").read_text()


def _data_path(path: str) -> Path:
    p = (SETTINGS.data_dir / path).resolve()
    if SETTINGS.data_dir.resolve() not in p.parents or not p.exists():
        raise HTTPException(404, "not found")
    return p


def _thumb_gray(p: Path, w: int) -> np.ndarray:
    from .ingest import load
    key = THUMBS / (hashlib.sha1(f"{p}|{p.stat().st_mtime}|{w}".encode()).hexdigest()[:20] + ".png")
    if key.exists():
        return cv2.imread(str(key), cv2.IMREAD_GRAYSCALE)
    img = load(p, dpi=60).pages[0].image
    s = w / img.shape[1]
    small = cv2.resize(img, (w, int(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(key), small)
    return small


@app.get("/api/thumb")
def thumb(path: str, w: int = 640):
    """Small preview of any data file (PDF page 1 or image), for the console animations."""
    img = _thumb_gray(_data_path(path), max(80, min(w, 1400)))
    ok, buf = cv2.imencode(".png", img)
    return Response(buf.tobytes(), media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/inspect")
def inspect(path: str, w: int = 640, n: int = 160):
    """Real ORB keypoints on the preview, normalised 0-1, so the console can draw what the fingerprint sees."""
    img = _thumb_gray(_data_path(path), max(80, min(w, 1400)))
    kps = cv2.ORB_create(nfeatures=2500, fastThreshold=10).detect(img, None)
    h, wd = img.shape
    # one strongest point per grid cell, so the overlay covers the whole drawing instead of one busy corner
    best: dict = {}
    for k in kps:
        cell = (int(k.pt[0] / wd * 30), int(k.pt[1] / h * 18))
        if cell not in best or k.response > best[cell].response:
            best[cell] = k
    pts = sorted(best.values(), key=lambda k: (k.pt[0], k.pt[1]))[:n]
    return {"w": wd, "h": h, "points": [[round(k.pt[0] / wd, 4), round(k.pt[1] / h, 4)] for k in pts]}


@app.get("/api/requests/{rid}/thumb")
def request_thumb(rid: str, w: int = 640):
    r = store.get_request(rid)
    if not r:
        raise HTTPException(404)
    p = Path(r["file_path"])
    img = _thumb_gray(p, max(80, min(w, 1400)))
    ok, buf = cv2.imencode(".png", img)
    return Response(buf.tobytes(), media_type="image/png")


@app.get("/api/stream")
async def stream(since: int = 0):
    """Server-sent events: live gate/sweep/audit events plus a machine meter every second."""
    async def gen():
        last = since or events.head()
        last_meter = 0.0
        yield f"event: hello\ndata: {json.dumps({'head': last})}\n\n"
        while True:
            for e in events.since(last):
                last = e["seq"]
                yield f"data: {json.dumps(e, default=str)}\n\n"
            if time.time() - last_meter > 1.0:
                last_meter = time.time()
                m = system.meter()
                m["sweep"] = dict(sweep.PROGRESS)
                yield f"event: meter\ndata: {json.dumps(m, default=str)}\n\n"
            await asyncio.sleep(0.12)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- gate

@app.post("/api/check")
async def check(file: UploadFile = File(...), requester: str = Form(...), recipient: str = Form(...)):
    name = Path(file.filename or "upload.bin").name
    dst = INBOX / f"{uuid.uuid4().hex[:8]}_{name}"
    with open(dst, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        return engine.check_outgoing(dst, requester, recipient, channel="dashboard")
    except ValueError as e:
        raise HTTPException(400, str(e))


class CheckPath(BaseModel):
    path: str
    requester: str
    recipient: str
    channel: str = "portal"


@app.post("/api/check_path")
def check_path(body: CheckPath):
    """For demos and the agent: check a file already on this machine (under data/)."""
    p = (SETTINGS.data_dir / body.path).resolve()
    if SETTINGS.data_dir.resolve() not in p.parents or not p.exists():
        raise HTTPException(404, "file must exist under data/")
    return engine.check_outgoing(p, body.requester, body.recipient, channel=body.channel or "portal")


@app.get("/api/requests")
def requests(limit: int = 50):
    return [{"id": r["id"], "created": r["created"], "file": r["file_name"], "status": r["status"], "p": r["p"],
             "jurisdiction": r["jurisdiction"], "requester": r["requester"], "recipient": r["recipient"],
             "summary": r["card"].get("summary", "")} for r in store.list_requests(limit)]


@app.get("/api/requests/{rid}")
def request_detail(rid: str):
    r = store.get_request(rid)
    if not r:
        raise HTTPException(404, "no such request")
    return r | {"events": store.events(50, ref=rid)}


@app.get("/api/requests/{rid}/match.png")
def match_png(rid: str):
    png = engine.match_picture(rid)
    if not png:
        raise HTTPException(404, "no match picture")
    return Response(png, media_type="image/png")


@app.get("/api/requests/{rid}/file")
def request_file(rid: str):
    r = store.get_request(rid)
    if not r:
        raise HTTPException(404)
    return FileResponse(r["file_path"])


class Respond(BaseModel):
    approver: str
    approve: bool
    note: str = ""


@app.post("/api/requests/{rid}/respond")
def respond(rid: str, body: Respond):
    return engine.respond(rid, body.approver, body.approve, body.note)


@app.get("/api/requests/{rid}/explain")
def explain(rid: str, use_model: bool = False):
    r = store.get_request(rid)
    if not r:
        raise HTTPException(404)
    card = r["card"]
    text = card["summary"]
    if use_model:
        try:
            text = llm.explain({k: card[k] for k in ("action", "reasons", "p", "jurisdiction", "ledger", "recipient", "file")})
        except llm.LLMUnavailable as e:
            text += f" (local model unavailable: {e})"
    return {"id": rid, "explanation": text}


# ---------------------------------------------------------------- perimeter map, workday, ask

@app.get("/api/graph")
def graph():
    return perimeter.graph()


class SimBody(BaseModel):
    n: int = 24
    seconds: float = 60
    attack_share: float = 0.3


@app.post("/api/simulate")
def simulate(body: SimBody):
    return perimeter.simulate(max(1, min(body.n, 200)), max(5, min(body.seconds, 900)), body.attack_share)


@app.get("/api/simulate/status")
def simulate_status():
    return perimeter.SIM


class AskBody(BaseModel):
    q: str


@app.post("/api/ask")
def ask(body: AskBody):
    return perimeter.ask(body.q)


# ---------------------------------------------------------------- mail

class MailBody(BaseModel):
    sender: str
    to: str
    subject: str = ""
    body: str = ""
    attach: str = ""  # a file under data/


@app.post("/api/mail/send")
def mail_send(m: MailBody):
    """Send a real email through Embargo's SMTP server and return the whole conversation."""
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = m.sender, m.to, m.subject or "(no subject)"
    msg.set_content(m.body or "Please see attached.")
    if m.attach:
        p = _data_path(m.attach)
        ext = p.suffix.lower()
        mt, sub = {".pdf": ("application", "pdf"), ".jpg": ("image", "jpeg"), ".jpeg": ("image", "jpeg"), ".png": ("image", "png")}.get(ext, ("application", "octet-stream"))
        msg.add_attachment(p.read_bytes(), maintype=mt, subtype=sub, filename=p.name)
    lines: list[str] = []

    class Rec(smtplib.SMTP):
        def _print_debug(self, *args):
            text = " ".join(str(a) for a in args)
            for kind, rx in (("C", r"^send: "), ("S", r"^reply: ")):
                if re.match(rx, text):
                    body = re.sub(rx, "", text).strip().strip("'\"")
                    body = body.replace("\\r\\n", "").replace("\r\n", "")
                    if body.startswith("b'") or body.startswith('b"'):
                        body = body[2:].rstrip("'\"")
                    body = body.replace("\\r\\n", "").strip()
                    if kind == "C" and not re.match(r"(?i)^(ehlo|helo|mail|rcpt|data|quit|rset|noop)\b", body):
                        body = f"‹the email itself: headers, text and attachment, {len(body):,} bytes›"
                    if body and not (kind == "S" and body.startswith("retcode")):
                        lines.append(f"{kind} {body}")
    result = {"ok": True, "code": 250, "reply": ""}
    try:
        with Rec("127.0.0.1", mailgate.PORT, timeout=120) as s:
            s.set_debuglevel(1)
            s.ehlo("workstation.kestrelprecision.local")
            s.send_message(msg)
            result["reply"] = "accepted"
    except smtplib.SMTPDataError as e:
        result = {"ok": False, "code": e.smtp_code, "reply": e.smtp_error.decode(errors="replace")}
    except (smtplib.SMTPException, OSError) as e:
        result = {"ok": False, "code": 0, "reply": f"mail server not reachable: {e}"}
    replies = [l[2:] for l in lines if l.startswith("S ") and re.match(r"^\d{3}", l[2:])]
    final = next((r for r in reversed(replies) if "Embargo" in r), replies[-1] if replies else result["reply"])
    ids = re.findall(r"EG-[0-9A-F]{6}", final)
    outcome = "refused" if final.startswith("550") else "held" if "held by Embargo" in final else "delivered"
    return {"outcome": outcome, "final": final, "ids": ids, "transcript": lines, "port": mailgate.PORT}


@app.get("/api/mail/boxes")
def mail_boxes():
    return mailgate.mailboxes()


# ---------------------------------------------------------------- sweep

class SweepBody(BaseModel):
    limit: int | None = None
    fresh: bool = False
    shuffle: bool = False


@app.post("/api/sweep")
def sweep_start(body: SweepBody):
    if sweep.PROGRESS["running"]:
        return {"ok": False, "message": "already running"}
    threading.Thread(target=sweep.run, kwargs={"limit": body.limit, "fresh": body.fresh, "shuffle": body.shuffle},
                     daemon=True).start()
    return {"ok": True}


@app.get("/api/sweep/progress")
def sweep_progress():
    return sweep.PROGRESS


@app.get("/api/sweep/latest")
def sweep_latest(limit: int = 25):
    return sweep.latest(limit) or {}


@app.get("/api/sweep/cells")
def sweep_cells(run_id: str):
    rows = store.conn().execute("select p, jurisdiction, exposure from sweep_items where run_id = ?", (run_id,)).fetchall()
    return ["c3" if r["exposure"] >= sweep.FLAG_AT else "c2" if r["jurisdiction"] != "EAR99" and r["p"] >= .5 else "c1"
            for r in rows]


class Quarantine(BaseModel):
    run_id: str
    file: str
    actor: str


@app.post("/api/sweep/quarantine")
def sweep_quarantine(body: Quarantine):
    return sweep.quarantine(body.run_id, body.file, body.actor)


# ---------------------------------------------------------------- audit + system

@app.get("/api/audit")
def audit(limit: int = 100):
    return store.events(limit)


@app.get("/api/audit/verify")
def audit_verify():
    return store.verify()


@app.post("/api/demo/tamper")
def demo_tamper():
    return store.tamper()


@app.post("/api/demo/reset")
def demo_reset():
    store.reset(keep_cache=True)
    for sub in ("delivered", "held"):  # demo mail generated by the app itself
        shutil.rmtree(mailgate.MAIL / sub, ignore_errors=True)
    shutil.rmtree(SETTINGS.data_dir / "webmail", ignore_errors=True)  # Kestrel Mail re-seeds its inbox
    return {"ok": True}


_LIVE = {"t": 0.0, "ok": False}


def _model_live() -> bool:
    """Is a model server actually answering? Cached for 15 s; the console never claims a model that isn't running."""
    import time as _t
    import urllib.request
    if _t.time() - _LIVE["t"] > 15:
        try:
            urllib.request.urlopen(SETTINGS.llm_base_url.rstrip("/") + "/models", timeout=1.5).read(200)
            _LIVE["ok"] = True
        except Exception:
            _LIVE["ok"] = False
        _LIVE["t"] = _t.time()
    return _LIVE["ok"]


@app.get("/api/system")
def sys_meter():
    return system.meter() | {"model": SETTINGS.llm_model, "model_url": SETTINGS.llm_base_url,
                             "reader": SETTINGS.reader, "model_live": _model_live()}


@app.post("/api/system/egress")
def sys_egress():
    return system.egress_test()


@app.get("/api/people")
def people():
    reg = registry()
    return {"staff": [vars(p) | {"cleared_programs": sorted(p.cleared_programs)} for p in reg.staff.values()],
            "recipients": [vars(r) for r in reg.recipients.values()]}


@app.get("/api/samples")
def samples():
    """Test files with who-sends-what-to-whom, for the dashboard's quick picker."""
    import csv
    rows = list(csv.DictReader(open(SETTINGS.data_dir / "outgoing" / "answer_key.csv")))
    return [{"path": r["file"], "set": r["set"], "variant": r["variant"], "requester": r["requester"],
             "recipient": r["recipient"], "note": r["note"]} for r in rows]


@app.get("/api/data/{path:path}")
def data_file(path: str):
    p = (SETTINGS.data_dir / path).resolve()
    if SETTINGS.data_dir.resolve() not in p.parents or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)
