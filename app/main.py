"""VoiceGetter web server.

Run:  python -m app.main            (listens on 0.0.0.0:8000)
Then open http://<this-computer-ip>:8000 on your phone.
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import pipeline

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("VOICEGETTER_DATA", ROOT.parent / "data"))
JOBS_DIR = DATA / "jobs"
KEEP_HOURS = float(os.environ.get("VOICEGETTER_KEEP_HOURS", "48"))
PASSWORD = os.environ.get("VOICEGETTER_PASSWORD", "")
COOKIES_FILE = DATA / "cookies.txt"
if not os.environ.get("VOICEGETTER_COOKIES") and COOKIES_FILE.is_file():
    os.environ["VOICEGETTER_COOKIES"] = str(COOKIES_FILE)

app = FastAPI(title="VoiceGetter")
jobs: dict[str, dict] = {}
work: "queue.Queue[str]" = queue.Queue()
lock = threading.Lock()


# ----------------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------------

def public(job: dict) -> dict:
    keys = ("id", "state", "progress", "step", "log", "error", "stats", "title", "created", "has_zip", "source")
    out = {k: job.get(k) for k in keys}
    with lock:
        out["queue_pos"] = list(work.queue).index(job["id"]) + 1 if job["id"] in work.queue else 0
    return out


def worker() -> None:
    while True:
        jid = work.get()
        job = jobs.get(jid)
        if not job or job["state"] == "cancelled":
            continue
        job["state"] = "running"

        def progress(frac: float, step: str) -> None:
            job["progress"] = round(max(job["progress"], min(frac, 1.0)), 4)
            job["step"] = step

        def log(msg: str) -> None:
            job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")

        def check() -> None:
            if job.get("cancel"):
                raise pipeline.Cancelled()

        rep = pipeline.Reporter(progress=progress, log=log, check=check)
        try:
            opt = pipeline.Options.from_dict(job["options"])
            src = Path(job["upload"]) if job.get("upload") else job["source"]
            res = pipeline.process(src, Path(job["dir"]), opt, rep, title=job.get("title"))
            job.update(state="done", progress=1.0, step="Done", stats=res.stats, title=res.title,
                       mp3=str(res.mp3), zip=str(res.zip) if res.zip else None, has_zip=bool(res.zip))
        except pipeline.Cancelled:
            job.update(state="cancelled", step="Cancelled")
            log("Cancelled")
        except Exception as e:  # noqa: BLE001
            job.update(state="error", error=str(e) or e.__class__.__name__, step="Failed")
            log("ERROR: " + (str(e) or e.__class__.__name__))
            traceback.print_exc()
        finally:
            if job.get("upload"):
                Path(job["upload"]).unlink(missing_ok=True)


def cleanup_old() -> None:
    if not JOBS_DIR.exists():
        return
    limit = time.time() - KEEP_HOURS * 3600
    for d in JOBS_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < limit and d.name not in jobs:
            shutil.rmtree(d, ignore_errors=True)
    for jid, job in list(jobs.items()):
        if job["created"] < limit and job["state"] in ("done", "error", "cancelled"):
            shutil.rmtree(job["dir"], ignore_errors=True)
            jobs.pop(jid, None)


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def auth(request: Request) -> None:
    if not PASSWORD:
        return
    given = request.headers.get("x-password") or request.query_params.get("key") or ""
    if given != PASSWORD:
        raise HTTPException(401, "Wrong password")


def get_job(jid: str) -> dict:
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "Job not found (the server may have restarted)")
    return job


def parse_time(s: str | None) -> float | None:
    """'1:02:03', '12:34' or '95' -> seconds."""
    if not s or not s.strip():
        return None
    try:
        parts = [float(p) for p in s.strip().split(":")]
    except ValueError:
        raise HTTPException(400, f"Bad time: {s!r}. Use h:mm:ss, mm:ss or seconds.")
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def config() -> dict:
    return {"password_required": bool(PASSWORD)}


@app.post("/api/jobs")
async def create_job(
    request: Request,
    url: str = Form(""),
    file: UploadFile | None = File(None),
    target: str = Form("female"),
    separate: bool = Form(True),
    strictness: float = Form(0.5),
    min_clip_sec: float = Form(1.0),
    gap_sec: float = Form(0.3),
    ref_start: str = Form(""),
    ref_end: str = Form(""),
    make_zip: bool = Form(False),
) -> dict:
    auth(request)
    url = url.strip()
    has_file = file is not None and file.filename
    if not url and not has_file:
        raise HTTPException(400, "Paste a YouTube link or choose a file.")
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "The link must start with http:// or https://")

    rs, re_ = parse_time(ref_start), parse_time(ref_end)
    if (rs is None) != (re_ is None):
        raise HTTPException(400, "Give both reference start and end, or neither.")
    options = dict(target=target, separate=separate, strictness=strictness, min_clip_sec=min_clip_sec,
                   gap_sec=gap_sec, ref_start=rs, ref_end=re_, make_zip=make_zip)
    try:
        pipeline.Options.from_dict(options)
    except ValueError as e:
        raise HTTPException(400, str(e))

    cleanup_old()
    jid = uuid.uuid4().hex[:12]
    jdir = JOBS_DIR / jid
    jdir.mkdir(parents=True, exist_ok=True)
    job = {"id": jid, "state": "queued", "progress": 0.0, "step": "Waiting in queue", "log": [],
           "error": None, "stats": None, "title": None, "created": time.time(), "dir": str(jdir),
           "options": options, "source": url, "upload": None, "has_zip": False}
    if has_file:
        suffix = Path(file.filename).suffix[:10] or ".bin"
        up = jdir / f"upload{suffix}"
        with up.open("wb") as f:
            while chunk := await file.read(1 << 20):
                f.write(chunk)
        job.update(upload=str(up), title=Path(file.filename).stem, source=file.filename)
    jobs[jid] = job
    with lock:
        work.put(jid)
    return public(job)


@app.post("/api/cookies")
async def set_cookies(request: Request) -> dict:
    """Save a Netscape cookies.txt (sent as the request body) for yt-dlp."""
    auth(request)
    text = (await request.body()).decode("utf-8", "replace")
    if not text.strip():
        COOKIES_FILE.unlink(missing_ok=True)
        os.environ.pop("VOICEGETTER_COOKIES", None)
        return {"ok": True, "cookies": False}
    if "\t" not in text:
        raise HTTPException(400, "This does not look like a cookies.txt file (Netscape format).")
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.write_text(text, encoding="utf-8")
    os.environ["VOICEGETTER_COOKIES"] = str(COOKIES_FILE)
    return {"ok": True, "cookies": True}


@app.get("/api/jobs")
def list_jobs(request: Request) -> list[dict]:
    auth(request)
    return [public(j) for j in sorted(jobs.values(), key=lambda j: -j["created"])]


@app.get("/api/jobs/{jid}")
def job_status(jid: str, request: Request) -> dict:
    auth(request)
    return public(get_job(jid))


@app.post("/api/jobs/{jid}/cancel")
def cancel(jid: str, request: Request) -> dict:
    auth(request)
    job = get_job(jid)
    if job["state"] == "queued":
        job.update(state="cancelled", step="Cancelled")
    elif job["state"] == "running":
        job["cancel"] = True
        job["step"] = "Cancelling..."
    return public(job)


@app.delete("/api/jobs/{jid}")
def delete(jid: str, request: Request) -> dict:
    auth(request)
    job = get_job(jid)
    if job["state"] == "running":
        raise HTTPException(409, "Cancel the job first.")
    job["state"] = "cancelled"
    shutil.rmtree(job["dir"], ignore_errors=True)
    jobs.pop(jid, None)
    return {"ok": True}


@app.get("/api/jobs/{jid}/{kind}")
def download(jid: str, kind: str, request: Request):
    auth(request)
    job = get_job(jid)
    if job["state"] != "done":
        raise HTTPException(409, "Not ready yet")
    path = job.get("mp3") if kind == "mp3" else job.get("zip") if kind == "zip" else None
    if not path or not Path(path).exists():
        raise HTTPException(404, "File not found")
    media = "audio/mpeg" if kind == "mp3" else "application/zip"
    inline = request.query_params.get("play") == "1"
    return FileResponse(path, media_type=media, filename=Path(path).name,
                        content_disposition_type="inline" if inline else "attachment")


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


threading.Thread(target=worker, daemon=True, name="voicegetter-worker").start()


def lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # No packet is sent; this only picks the outgoing interface.
        return s.getsockname()[0]
    except OSError:
        return "<this-computer-ip>"
    finally:
        s.close()


def main() -> None:
    import uvicorn
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    print(f"\nVoiceGetter is running.\n  On this computer: http://localhost:{port}"
          f"\n  On your phone (same Wi-Fi): http://{lan_ip()}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
