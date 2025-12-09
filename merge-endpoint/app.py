from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import FileResponse
from pydub import AudioSegment, silence
import tempfile, os, uuid, requests, json, time, traceback
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()
APP_VERSION = "merge-async-v1-2025-12-09"

# Wo fertige MP3s liegen (Render erlaubt /tmp)
STORE_DIR = "/tmp/merged_store"
os.makedirs(STORE_DIR, exist_ok=True)

# Job-Status im RAM (bei Neustart weg – aber ok für euren Workflow)
jobs = {}  # job_id -> dict(status, created_at, done_at, error, out_path)

# ThreadPool für Background-Jobs
executor = ThreadPoolExecutor(max_workers=2)


@app.get("/health")
def health():
    return {"ok": True}

@app.get("/version")
def version():
    return {"version": APP_VERSION}


def _download(url, path):
    r = requests.get(url, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"Download failed: {url} status={r.status_code}")
    with open(path, "wb") as f:
        f.write(r.content)
    size = os.path.getsize(path)
    if size < 1000:
        raise RuntimeError("Downloaded file too small, likely not audio")
    return size


def _merge_job(job_id: str, payload: dict):
    try:
        jobs[job_id]["status"] = "running"

        a_url = payload.get("a_url")
        b_url = payload.get("b_url")
        min_silence_ms = int(payload.get("min_silence_ms", 2000))
        silence_thresh_dbfs = int(payload.get("silence_thresh_dbfs", -35))

        if not a_url or not b_url:
            raise RuntimeError("a_url and b_url are required")

        print("MERGE job start", job_id)
        print("params", {
            "min_silence_ms": min_silence_ms,
            "silence_thresh_dbfs": silence_thresh_dbfs
        })

        with tempfile.TemporaryDirectory() as td:
            a_path = os.path.join(td, "a.mp3")
            b_path = os.path.join(td, "b.mp3")

            sa = _download(a_url, a_path)
            sb = _download(b_url, b_path)
            print("download sizes", sa, sb)

            a = AudioSegment.from_file(a_path).set_frame_rate(22050).set_channels(1)
            b = AudioSegment.from_file(b_path).set_frame_rate(22050).set_channels(1)

            def chunks_from_nonsilent(audio):
                ranges = silence.detect_nonsilent(
                    audio,
                    min_silence_len=min_silence_ms,
                    silence_thresh=silence_thresh_dbfs
                )
                # extrem viele Mini-Segmente → lieber nicht splitten
                if len(ranges) > 200:
                    return [audio]
                return [audio[s:e] for s, e in ranges]

            print("detect chunks")
            a_chunks = chunks_from_nonsilent(a)
            b_chunks = chunks_from_nonsilent(b)
            print("chunks", len(a_chunks), len(b_chunks))

            out = AudioSegment.silent(0)
            i = 0
            while i < len(a_chunks) or i < len(b_chunks):
                if i < len(a_chunks):
                    out += a_chunks[i]
                if i < len(b_chunks):
                    out += b_chunks[i]
                i += 1

        out_path = os.path.join(STORE_DIR, f"{job_id}.mp3")
        print("export", out_path)
        out.export(out_path, format="mp3")
        print("export done size", os.path.getsize(out_path))

        jobs[job_id]["status"] = "done"
        jobs[job_id]["done_at"] = time.time()
        jobs[job_id]["out_path"] = out_path

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["trace"] = traceback.format_exc()
        print("MERGE job error", job_id, str(e))


@app.post("/merge_async")
def merge_async(payload=Body(...)):
    # Langdock kann JSON als String schicken
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            raise HTTPException(400, "Body must be JSON or JSON-string")

    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        "status": "queued",
        "created_at": time.time(),
        "done_at": None,
        "error": None,
        "out_path": None
    }

    # Job im Hintergrund starten
    executor.submit(_merge_job, job_id, payload)

    return {
        "job_id": job_id,
        "status_url": f"https://merge-endpoint.onrender.com/status/{job_id}"
    }


@app.get("/status/{job_id}")
def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")

    resp = {
        "job_id": job_id,
        "status": job["status"],
        "created_at": job["created_at"],
        "done_at": job["done_at"],
        "error": job["error"]
    }

    if job["status"] == "done":
        resp["download_url"] = f"https://merge-endpoint.onrender.com/download/{job_id}"

    return resp


@app.get("/download/{job_id}")
def download(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job["status"] != "done" or not job["out_path"]:
        raise HTTPException(400, "job not finished")

    path = job["out_path"]
    if not os.path.exists(path):
        raise HTTPException(404, "file missing on disk")

    return FileResponse(path, media_type="audio/mpeg", filename="merged.mp3")
