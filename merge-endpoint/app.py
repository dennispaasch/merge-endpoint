from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import FileResponse
from pydub import AudioSegment, silence
from pydub.generators import Sine
import tempfile, os, uuid, requests, json, time, traceback
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()
APP_VERSION = "merge-async-v6-bleep-on-switch-2025-12-10"

# Speicherort für fertige MP3s
STORE_DIR = "/tmp/merged_store"
os.makedirs(STORE_DIR, exist_ok=True)

# Job-Status (im RAM)
jobs = {}  # job_id -> dict(status, created_at, done_at, error, out_path)

# Background-Threads
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
        raise RuntimeError("Downloaded file too small (not audio?)")
    return size


def _merge_job(job_id: str, payload: dict):
    try:
        jobs[job_id]["status"] = "running"

        # Pflicht
        a_url = payload.get("a_url")
        b_url = payload.get("b_url")
        if not a_url or not b_url:
            raise RuntimeError("a_url and b_url are required")

        # Silence/Chunk-Parameter
        min_silence_ms = int(payload.get("min_silence_ms", 2000))
        silence_thresh_dbfs = int(payload.get("silence_thresh_dbfs", -35))

        # Tail/Satzende retten
        keep_silence_ms = int(payload.get("keep_silence_ms", 300))  # hinten dran
        pre_pad_ms = int(payload.get("pre_pad_ms", 50))             # vorne minimal Luft

        # Start / Gap
        start_with = payload.get("start_with", "a")  # "a" oder "b"
        gap_ms = int(payload.get("gap_ms", 300))

        # Pattern aus Skript
        pattern = payload.get("pattern")  # optional: ["A","B","A"...] oder None
        if pattern is not None and not isinstance(pattern, list):
            raise RuntimeError("pattern must be a list like ['A','B',...]")

        print("MERGE job start", job_id)
        print("params", {
            "min_silence_ms": min_silence_ms,
            "silence_thresh_dbfs": silence_thresh_dbfs,
            "keep_silence_ms": keep_silence_ms,
            "pre_pad_ms": pre_pad_ms,
            "start_with": start_with,
            "gap_ms": gap_ms,
            "pattern_len": len(pattern) if pattern else None
        })

        with tempfile.TemporaryDirectory() as td:
            a_path = os.path.join(td, "a.mp3")
            b_path = os.path.join(td, "b.mp3")

            sa = _download(a_url, a_path)
            sb = _download(b_url, b_path)
            print("download sizes", sa, sb)

            a = AudioSegment.from_file(a_path)
            b = AudioSegment.from_file(b_path)

            # SPEED-FIX: 16kHz Mono reicht für Sprachpausen und ist viel schneller
            a = a.set_frame_rate(16000).set_channels(1)
            b = b.set_frame_rate(16000).set_channels(1)

            def chunks_from_nonsilent(audio):
                ranges = silence.detect_nonsilent(
                    audio,
                    min_silence_len=min_silence_ms,
                    silence_thresh=silence_thresh_dbfs,
                    seek_step=10
                )

                # Cap nur noch OHNE pattern
                if (not pattern) and len(ranges) > 80:
                    print("too many chunks, skipping split (no pattern)")
                    return [audio]

                out_chunks = []
                audio_len = len(audio)
                for s, e in ranges:
                    s2 = max(0, s - pre_pad_ms)
                    e2 = min(audio_len, e + keep_silence_ms)
                    out_chunks.append(audio[s2:e2])
                return out_chunks

            print("detect chunks")
            a_chunks = chunks_from_nonsilent(a)
            b_chunks = chunks_from_nonsilent(b)
            print("chunks", len(a_chunks), len(b_chunks))

            # Guard gegen Pattern/Chunk-Mismatch
            if pattern:
                expected_a = sum(1 for p in pattern if str(p).upper().strip() == "A")
                expected_b = sum(1 for p in pattern if str(p).upper().strip() == "B")
                if abs(expected_a - len(a_chunks)) > 1 or abs(expected_b - len(b_chunks)) > 1:
                    raise RuntimeError(
                        f"Pattern/Chunk mismatch: expected A={expected_a}, B={expected_b} "
                        f"but got A_chunks={len(a_chunks)}, B_chunks={len(b_chunks)}. "
                        f"Check TTS breaks or silence params."
                    )

            # Pause zwischen Segmenten
            gap = AudioSegment.silent(duration=gap_ms)

            # --- Bleep nur beim Sprecherwechsel ---
            bleep_hz = int(payload.get("bleep_hz", 1000))
            bleep_ms = int(payload.get("bleep_ms", 120))
            bleep_gain_db = int(payload.get("bleep_gain_db", -6))
            bleep = Sine(bleep_hz).to_audio_segment(duration=bleep_ms).apply_gain(bleep_gain_db)

            out = AudioSegment.silent(0)

            last_speaker = None  # merken, wer zuletzt gesprochen hat

            def add(seg, speaker):
                nonlocal out, last_speaker
                # Bleep nur wenn Sprecherwechsel
                if last_speaker is not None and speaker != last_speaker:
                    out += bleep
                out += seg
                out += gap
                last_speaker = speaker

            # Pattern-gesteuertes Mergen
            a_i = 0
            b_i = 0

            if pattern:
                for p in pattern:
                    sp = str(p).upper().strip()
                    if sp == "A":
                        if a_i < len(a_chunks):
                            add(a_chunks[a_i], "A"); a_i += 1
                    elif sp == "B":
                        if b_i < len(b_chunks):
                            add(b_chunks[b_i], "B"); b_i += 1
                    else:
                        continue

                # Reste hinten dranhängen (falls Pattern zu kurz war)
                while a_i < len(a_chunks) or b_i < len(b_chunks):
                    if start_with.lower() == "a":
                        if a_i < len(a_chunks):
                            add(a_chunks[a_i], "A"); a_i += 1
                        if b_i < len(b_chunks):
                            add(b_chunks[b_i], "B"); b_i += 1
                    else:
                        if b_i < len(b_chunks):
                            add(b_chunks[b_i], "B"); b_i += 1
                        if a_i < len(a_chunks):
                            add(a_chunks[a_i], "A"); a_i += 1

            else:
                # Fallback: altes Verhalten (start_with + alternierend)
                i = 0
                while i < len(a_chunks) or i < len(b_chunks):
                    if start_with.lower() == "a":
                        if i < len(a_chunks):
                            add(a_chunks[i], "A")
                        if i < len(b_chunks):
                            add(b_chunks[i], "B")
                    else:
                        if i < len(b_chunks):
                            add(b_chunks[i], "B")
                        if i < len(a_chunks):
                            add(a_chunks[i], "A")
                    i += 1

            # Export nach /tmp/merged_store
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
    # Langdock schickt manchmal JSON als String
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

    if job["status"] == "error":
        resp["trace"] = job.get("trace")

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
